"""Versioned knowledge base in Milvus.

    collection <KB_NAME>__<version>   one per build, never modified afterwards
    alias      <KB_NAME>              points at the active version (switched atomically)
    property   kb.meta                build metadata incl. embedding model + dimensions

Searches resolve the alias once, so one question never mixes versions, and refuse to run if
the collection was built with a different embedding model/dimensions than the query uses.
pymilvus is synchronous; async callers use asyncio.to_thread.
"""

import json
from dataclasses import dataclass
from typing import Any

from pymilvus import DataType, MilvusClient, MilvusException

META = "kb.meta"
FIELDS = ["doc_id", "title", "source", "section", "page", "text", "classification",
          "access_scope"]
_MAX = {"id": 200, "doc_id": 128, "title": 1024, "source": 1024, "section": 1024,
        "text": 16384, "classification": 64, "access_scope": 128}


class KnowledgeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    source: str
    section: str
    page: int | None
    text: str
    score: float


def _fit(value: Any, field: str) -> str:
    return str(value or "").encode("utf-8")[: _MAX[field]].decode("utf-8", errors="ignore")


class KnowledgeStore:
    def __init__(self, uri: str, name: str, token: str | None = None,
                 index_type: str = "HNSW") -> None:
        self.name = name
        self._uri, self._token, self._index_type = uri, token, index_type
        self._client: MilvusClient | None = None

    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            self._client = MilvusClient(uri=self._uri, **({"token": self._token}
                                                           if self._token else {}))
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def collection(self, version: str) -> str:
        return f"{self.name}__{version}"

    # ---- versions -------------------------------------------------------------------------

    def active_version(self) -> str | None:
        try:
            listed = self.client.list_aliases()
            aliases = listed.get("aliases", []) if isinstance(listed, dict) else listed
            if self.name not in aliases:  # avoids pymilvus logging an error traceback
                return None
            target = self.client.describe_alias(self.name).get("collection_name", "")
        except MilvusException:
            return None
        prefix = f"{self.name}__"
        return target[len(prefix):] if target.startswith(prefix) else None

    def metadata(self, version: str) -> dict:
        props = self.client.describe_collection(self.collection(version)).get("properties") or {}
        return json.loads(props.get(META, "{}"))

    def versions(self) -> list[dict]:
        active = self.active_version()
        prefix = f"{self.name}__"
        return [{"version": n[len(prefix):], "active": n[len(prefix):] == active,
                 **self.metadata(n[len(prefix):])}
                for n in sorted(self.client.list_collections()) if n.startswith(prefix)]

    def activate(self, version: str) -> str | None:
        name = self.collection(version)
        if not self.client.has_collection(name):
            raise ValueError(f"knowledge version {version!r} does not exist")
        previous = self.active_version()
        self.client.load_collection(name)
        if previous is None:
            self.client.create_alias(name, self.name)
        else:
            self.client.alter_alias(name, self.name)
        return previous

    def drop(self, version: str) -> None:
        if version == self.active_version():
            raise ValueError("cannot drop the active version; activate another one first")
        self.client.drop_collection(self.collection(version))

    # ---- build ----------------------------------------------------------------------------

    def create(self, version: str, dimensions: int, meta: dict) -> None:
        name = self.collection(version)
        if self.client.has_collection(name):
            raise ValueError(f"knowledge version {version!r} already exists")
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=_MAX["id"])
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dimensions)
        for f in FIELDS:
            if f != "page":
                schema.add_field(f, DataType.VARCHAR, max_length=_MAX[f])
        schema.add_field("page", DataType.INT64)
        index = self.client.prepare_index_params()
        index.add_index("embedding", index_type=self._index_type, metric_type="COSINE",
                        params={"M": 16, "efConstruction": 200}
                        if self._index_type == "HNSW" else {})
        self.client.create_collection(name, schema=schema, index_params=index)
        self.client.alter_collection_properties(name, properties={
            META: json.dumps(meta, ensure_ascii=False)})

    def insert(self, version: str, rows: list[dict]) -> int:
        name = self.collection(version)
        data = [{"id": _fit(r["id"], "id"), "embedding": r["embedding"],
                 "page": int(r.get("page") or -1),
                 **{f: _fit(r.get(f), f) for f in FIELDS if f != "page"}} for r in rows]
        for i in range(0, len(data), 500):
            self.client.insert(name, data[i:i + 500])
        self.client.flush(name)
        self.client.load_collection(name)
        return int(self.client.query(name, filter="", output_fields=["count(*)"])[0]["count(*)"])

    # ---- search ---------------------------------------------------------------------------

    def resolve(self, expected_embedding: dict, version: str | None = None) -> tuple[str, dict]:
        """Version to search (pinned or active) after checking embedding compatibility."""
        try:
            version = version or self.active_version()
            if version is None:
                raise KnowledgeUnavailable("knowledge base has no active version; run "
                                           "scripts/build_knowledge.py")
            meta = self.metadata(version)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(f"Milvus unavailable: {exc}") from exc
        if meta.get("embedding") != expected_embedding:
            raise KnowledgeUnavailable(
                f"version {version} was built with {meta.get('embedding')}, queries use "
                f"{expected_embedding}; rebuild the knowledge base")
        return version, meta

    def search(self, version: str, vector: list[float], top_k: int, filter_expr: str = ""
               ) -> list[Hit]:
        res = self.client.search(
            self.collection(version), data=[vector], anns_field="embedding", limit=top_k,
            filter=filter_expr, output_fields=FIELDS,
            search_params={"metric_type": "COSINE",
                           "params": {"ef": max(64, 4 * top_k)}
                           if self._index_type == "HNSW" else {}})
        return [Hit(chunk_id=h["id"], score=float(h["distance"]), doc_id=h["entity"]["doc_id"],
                    title=h["entity"]["title"], source=h["entity"]["source"],
                    section=h["entity"]["section"], text=h["entity"]["text"],
                    page=h["entity"]["page"] if h["entity"]["page"] >= 0 else None)
                for h in res[0]]
