"""Offline knowledge-base tool (run by operators, never exposed through the API).

    industrial-ai-kb checksum path/to/manifest.json [--write]   # prepare a release
    industrial-ai-kb build path/to/manifest.json [--no-activate] [--replace]
    industrial-ai-kb list
    industrial-ai-kb status
    industrial-ai-kb activate <version>        # also used for rollback
    industrial-ai-kb drop <version>            # refuses the active version
    industrial-ai-kb search "question" [--top-k 5] [--roles a,b]

Uses the same EMBEDDING_* / MILVUS_* / KB_* settings as the API, which guarantees index
build and queries share one embedding configuration.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from industrial_ai.core.config import Settings
from industrial_ai.core.logging import configure_logging
from industrial_ai.integrations.embedding_client import EmbeddingClient, EmbeddingConfig
from industrial_ai.knowledge.builder import build_knowledge_base
from industrial_ai.knowledge.manifest import sha256_file
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore
from industrial_ai.knowledge.retriever import KnowledgeRetriever


def build_store(settings: Settings) -> MilvusKnowledgeStore:
    if not settings.milvus_uri:
        sys.exit("MILVUS_URI is not set")
    return MilvusKnowledgeStore(
        settings.milvus_uri, alias=settings.kb_alias,
        token=settings.milvus_token.get_secret_value() if settings.milvus_token else None,
        db_name=settings.milvus_db_name, index_type=settings.kb_index_type,
    )


def build_embedder(settings: Settings) -> EmbeddingClient:
    config = EmbeddingConfig.from_settings(settings)
    if config is None:
        sys.exit("EMBEDDING_PROVIDER is 'none'; configure the GLM embedding model first")
    return EmbeddingClient(config)


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _checksum(manifest_path: Path, write: bool) -> None:
    """Fill in / check document SHA-256 values while preparing an approved release."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed = []
    for doc in data.get("documents", []):
        actual = sha256_file(manifest_path.parent / doc["path"])
        if doc.get("sha256") != actual:
            changed.append(doc["doc_id"])
            doc["sha256"] = actual
    if write:
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    _print({"updated" if write else "mismatched": changed})


async def _main(args: argparse.Namespace, settings: Settings) -> None:
    if args.command == "checksum":
        _checksum(Path(args.manifest), args.write)
        return
    store = build_store(settings)
    try:
        if args.command == "build":
            embedder = build_embedder(settings)
            try:
                report = await build_knowledge_base(
                    Path(args.manifest), store, embedder, chunk_chars=settings.kb_chunk_chars,
                    chunk_overlap=settings.kb_chunk_overlap, activate=not args.no_activate,
                    replace=args.replace)
            finally:
                await embedder.aclose()
            _print(report.to_dict())
        elif args.command == "list":
            _print(store.versions())
        elif args.command == "status":
            _print({"alias": store.alias, "active_version": store.active_version()})
        elif args.command == "activate":
            previous = store.activate(args.version)
            _print({"active_version": args.version, "previous_version": previous})
        elif args.command == "drop":
            store.drop(args.version)
            _print({"dropped": args.version})
        elif args.command == "search":
            embedder = build_embedder(settings)
            try:
                retriever = KnowledgeRetriever(
                    store, embedder, pinned_version=settings.knowledge_index_version,
                    top_k=args.top_k, min_score=settings.kb_min_score,
                    allowed_classifications=settings.kb_allowed_classifications)
                roles = tuple(r for r in (args.roles or "").split(",") if r)
                result = await retriever.search(args.query, roles=roles)
            finally:
                await embedder.aclose()
            _print({"kb_version": result.kb_version,
                    "hits": [{**h.__dict__, "text": h.text[:200]} for h in result.hits]})
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="industrial-ai-kb")
    sub = parser.add_subparsers(dest="command", required=True)
    cs = sub.add_parser("checksum", help="compute document SHA-256 for a manifest")
    cs.add_argument("manifest")
    cs.add_argument("--write", action="store_true", help="write the values into the manifest")
    b = sub.add_parser("build", help="build a knowledge version from a manifest")
    b.add_argument("manifest")
    b.add_argument("--no-activate", action="store_true", help="build without switching alias")
    b.add_argument("--replace", action="store_true", help="rebuild an existing inactive version")
    sub.add_parser("list")
    sub.add_parser("status")
    a = sub.add_parser("activate", help="switch the alias (also used for rollback)")
    a.add_argument("version")
    d = sub.add_parser("drop")
    d.add_argument("version")
    s = sub.add_parser("search", help="debug retrieval")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=5)
    s.add_argument("--roles", default="")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.app_env, stream=sys.stderr)
    try:
        asyncio.run(_main(args, settings))
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
