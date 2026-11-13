#!/usr/bin/env python
"""Initialise / update the Milvus knowledge base with GLM embeddings (offline).

  python scripts/build_knowledge.py                       # build data/knowledge, activate it
  python scripts/build_knowledge.py --corpus /path/docs   # your documents (manifest optional;
                                                          # .md .txt .pdf .docx)
  python scripts/build_knowledge.py --dry-run             # show chunks, no embedding/Milvus
  python scripts/build_knowledge.py list                  # versions and the active one
  python scripts/build_knowledge.py activate <version>    # switch / roll back
  python scripts/build_knowledge.py drop <version>
  python scripts/build_knowledge.py search "剂量率单位换算"

Needs MILVUS_URI and EMBEDDING_PROVIDER (+ key). The API must use the same EMBEDDING_MODEL
and EMBEDDING_DIMENSIONS; it refuses to search a version built with different ones.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

import _common

from app.rag.embeddings import GLMEmbeddings
from app.rag.ingest import build, load_manifest, split, verify
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("command", nargs="?", default="build",
                   choices=["build", "list", "activate", "drop", "search"])
    p.add_argument("arg", nargs="?", help="version (activate/drop) or query (search)")
    p.add_argument("--corpus", type=Path, help="corpus directory (default KB_CORPUS_PATH, "
                   "data/knowledge)")
    p.add_argument("--version", help="override the version name")
    p.add_argument("--no-activate", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="only parse and chunk")
    args = p.parse_args()
    s = _common.settings(args)
    args.corpus = args.corpus or s.kb_corpus_path

    if args.command == "build" and args.dry_run:
        manifest = load_manifest(args.corpus)
        files = verify(manifest, args.corpus)
        for d in manifest.documents:
            chunks = split(d, files[d.doc_id], s.kb_chunk_size, s.kb_chunk_overlap)
            print(f"{d.doc_id}: {len(chunks)} chunks")
            for c in chunks[:3]:
                print(f"   [{c.metadata['section'] or '-'}] {c.page_content[:70]!r}")
        print(f"version would be: {args.version or manifest.version}")
        return

    if not s.milvus_uri:
        sys.exit("MILVUS_URI is not set")
    store = KnowledgeStore(s.milvus_uri, s.kb_name,
                           s.milvus_token.get_secret_value() if s.milvus_token else None)
    try:
        if args.command == "list":
            print(json.dumps(store.versions(), ensure_ascii=False, indent=2))
        elif args.command in ("activate", "drop"):
            if not args.arg:
                sys.exit(f"usage: build_knowledge.py {args.command} <version>")
            if args.command == "activate":
                print(f"active: {args.arg} (previous: {store.activate(args.arg)})")
            else:
                store.drop(args.arg)
                print(f"dropped {args.arg}")
        else:
            embeddings = GLMEmbeddings.from_settings(s)
            if embeddings is None:
                sys.exit("EMBEDDING_PROVIDER is none; configure the GLM embedding first")
            if args.command == "search":
                from app.ingestion.pipeline import run

                retriever = Retriever(store, embeddings, top_k=5, min_score=s.kb_min_score,
                                      classifications=tuple(s.kb_classifications))
                version, hits = run(retriever.search(args.arg or ""))
                print(f"version {version}")
                for h in hits:
                    print(f"  {h.score:.3f}  {h.doc_id} [{h.section}]  {h.text[:60]!r}")
            else:
                result = build(args.corpus, store, embeddings, chunk_size=s.kb_chunk_size,
                               overlap=s.kb_chunk_overlap, activate=not args.no_activate,
                               version=args.version)
                print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
