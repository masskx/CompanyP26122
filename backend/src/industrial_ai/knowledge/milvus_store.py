"""Milvus storage for versioned knowledge collections.

Layout:
    collection  <alias>__<version>   one per knowledge release, immutable once built
    alias       <alias>              points at the active release; switched atomically
    property    kb.meta              JSON build metadata (embedding model/dimensions, chunker,
                                     manifest checksum, counts) stored on each collection

Queries resolve the alias to a concrete collection once and use only that collection, so
a single answer never mixes releases (FR-KB-06). Old collections are kept for rollback.

pymilvus is synchronous; async callers wrap calls in asyncio.to_thread.
"""

import json
import logging
from typing import Any

from pymilvus import DataType, MilvusClient, MilvusException

from industrial_ai.knowledge.chunker import Chunk

logger = logging.getLogger(__name__)

META_KEY = "kb.meta"
OUTPUT_FIELDS = ["doc_id", "title", "source", "section", "page", "chunk_index", "text",
                 "doc_version", "classification", "access_scope"]
_VARCHAR = {"id": 200, "doc_id": 128, "title": 1024, "source": 1024, "section": 1024,
            "text": 16384, "doc_version": 128, "classification": 64, "access_scope": 128}


def _fit(value: str | None, field: str) -> str:
    """Truncate to the field's byte limit on a character boundary."""
    data = (value or "").encode("utf-8")[: _VARCHAR[field]]
    return data.decode("utf-8", errors="ignore")


class MilvusKnowledgeStore:
    def __init__(self, uri: str, *, alias: str, token: str | None = None,
                 db_name: str = "default", index_type: str = "HNSW") -> None:
        self._uri = uri
        self._token = token
        self._db_name = db_name
        self.alias = alias
        self._index_type = index_type
        self._client: MilvusClient | None = None

    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"uri": self._uri}
            if self._token:
                kwargs["token"] = self._token
            if self._db_name and self._db_name != "default":
                kwargs["db_name"] = self._db_name
            self._client = MilvusClient(**kwargs)
        return self._client

    def collection_name(self, version: str) -> str:
        return f"{self.alias}__{version}"

    def _version_of(self, collection: str) -> str | None:
        prefix = f"{self.alias}__"
        return collection[len(prefix):] if collection.startswith(prefix) else None

    # ---- build ------------------------------------------------------------------------

    def exists(self, version: str) -> bool:
        return bool(self.client.has_collection(self.collection_name(version)))

    def create_version(self, version: str, dimensions: int, meta: dict) -> str:
        name = self.collection_name(version)
        if self.client.has_collection(name):
            raise ValueError(f"knowledge version {version!r} already exists")
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False,
                                            description=f"knowledge base {version}")
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=_VARCHAR["id"])
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dimensions)
        for field in ("doc_id", "title", "source", "section", "text", "doc_version",
                      "classification", "access_scope"):
            schema.add_field(field, DataType.VARCHAR, max_length=_VARCHAR[field])
        schema.add_field("page", DataType.INT64)
        schema.add_field("chunk_index", DataType.INT64)

        index = self.client.prepare_index_params()
        params = {"M": 16, "efConstruction": 200} if self._index_type == "HNSW" else {}
        index.add_index("embedding", index_type=self._index_type, metric_type="COSINE",
                        params=params)
        index.add_index("classification", index_type="INVERTED")
        index.add_index("access_scope", index_type="INVERTED")
        self.client.create_collection(name, schema=schema, index_params=index)
        self.client.alter_collection_properties(
            name, properties={META_KEY: json.dumps(meta, ensure_ascii=False)})
        return name

    def insert(self, version: str, chunks: list[Chunk], vectors: list[list[float]],
               batch_size: int = 500) -> None:
        name = self.collection_name(version)
        rows = [{
            "id": _fit(c.chunk_id, "id"), "embedding": v, "doc_id": _fit(c.doc_id, "doc_id"),
            "title": _fit(c.title, "title"), "source": _fit(c.source, "source"),
            "section": _fit(c.section, "section"), "text": _fit(c.text, "text"),
            "doc_version": _fit(c.doc_version, "doc_version"),
            "classification": _fit(c.classification, "classification"),
            "access_scope": _fit(c.access_scope, "access_scope"),
            "page": c.page if c.page is not None else -1, "chunk_index": c.chunk_index,
        } for c, v in zip(chunks, vectors, strict=True)]
        for i in range(0, len(rows), batch_size):
            self.client.insert(name, rows[i:i + batch_size])

    def finalize(self, version: str) -> int:
        """Flush, load and return the number of stored chunks."""
        name = self.collection_name(version)
        self.client.flush(name)
        self.client.load_collection(name)
        rows = self.client.query(name, filter="", output_fields=["count(*)"])
        return int(rows[0]["count(*)"])

    def drop(self, version: str) -> None:
        if self.active_version() == version:
            raise ValueError(f"version {version!r} is active; activate another version first")
        self.client.drop_collection(self.collection_name(version))

    # ---- alias ------------------------------------------------------------------------

    def active_version(self) -> str | None:
        try:
            info = self.client.describe_alias(self.alias)
        except MilvusException:
            return None
        return self._version_of(info.get("collection_name", ""))

    def activate(self, version: str) -> str | None:
        """Point the alias at `version` atomically. Returns the previously active version."""
        name = self.collection_name(version)
        if not self.client.has_collection(name):
            raise ValueError(f"knowledge version {version!r} does not exist")
        previous = self.active_version()
        if previous is None:
            self.client.create_alias(name, self.alias)
        else:
            self.client.alter_alias(name, self.alias)
        self.client.load_collection(name)
        logger.info("knowledge version activated", extra={"fields": {
            "version": version, "previous": previous}})
        return previous

    # ---- read -------------------------------------------------------------------------

    def metadata(self, version: str) -> dict:
        info = self.client.describe_collection(self.collection_name(version))
        raw = (info.get("properties") or {}).get(META_KEY)
        return json.loads(raw) if raw else {}

    def versions(self) -> list[dict]:
        active = self.active_version()
        out = []
        for name in sorted(self.client.list_collections()):
            version = self._version_of(name)
            if version is None:
                continue
            meta = self.metadata(version)
            out.append({"version": version, "active": version == active,
                        "built_at": meta.get("built_at"), "chunks": meta.get("chunks"),
                        "documents": meta.get("documents"), "embedding": meta.get("embedding")})
        return out

    def search(self, version: str, vector: list[float], *, top_k: int, filter_expr: str
               ) -> list[dict]:
        result = self.client.search(
            self.collection_name(version), data=[vector], anns_field="embedding", limit=top_k,
            filter=filter_expr, output_fields=OUTPUT_FIELDS,
            search_params={"metric_type": "COSINE",
                           "params": {"ef": max(64, top_k * 4)} if self._index_type == "HNSW"
                           else {}},
        )
        return [{"id": hit["id"], "score": float(hit["distance"]), **hit["entity"]}
                for hit in result[0]]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
