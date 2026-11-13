# Preparing the knowledge base

The assistant answers technical questions only from this curated, versioned corpus. There is no
upload or online editing. A release is built offline into Milvus and switched atomically.

## 1. Collect the documents

Put the files in a corpus folder: this directory, or another one via `KB_CORPUS_PATH` or
`--corpus`.

| Format | How it is split |
| --- | --- |
| `.md` | by `#` / `##` / `###` headings; the section path is kept for citations |
| `.docx` (Word) | heading styles (标题 1–3 / Heading 1–3) become sections; tables become "cell \| cell" lines |
| `.pdf` | by page; the page number is kept |
| `.txt` | plain text |

Word and PDF support needs `pip install -e ".[docs]"`.

**Tips:**
- Use real heading styles in Word, so answers can cite "规程 > 3 故障处理".
- Scanned PDFs need OCR first; image-only pages contain no text.
- One topic per document works best: a procedure, a manual chapter, a FAQ.
- Remove drafts and duplicates; the newest version of a procedure should be the only one.

## 2. Describe the release in `manifest.json` (recommended)

```json
{
  "version": "kb_2026_11_20",
  "title": "监测站运维知识库",
  "documents": [
    {"doc_id": "battery-maintenance", "path": "docs/备电维护规程.docx",
     "title": "备电维护规程", "source": "运维部 2026 版",
     "classification": "internal", "access_scope": "all",
     "sha256": "<sha256sum of the file>"}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `version` | the release name (letters, digits and `_`); one Milvus collection per version |
| `doc_id` | stable ID used in citations |
| `title`, `source` | shown in citations |
| `classification` | `public` / `internal` (`KB_CLASSIFICATIONS` decides what is searchable), or e.g. `restricted` to keep a file out of answers |
| `access_scope` | `all` for now; role-based scopes once users have roles |
| `sha256` | the build refuses a file whose content changed after review |

Without a manifest, every supported file is indexed and the version is a timestamp.

To compute checksums (PowerShell: `Get-FileHash -Algorithm SHA256 <file>`):

```bash
sha256sum docs/*
```

## 3. Choose the embedding model

| Stage | Settings |
| --- | --- |
| Development | `EMBEDDING_PROVIDER=zhipu` + `GLM_API_KEY` (public API; sends only the corpus text) |
| On-site | `EMBEDDING_PROVIDER=local` + `EMBEDDING_BASE_URL` (OpenAI-compatible `/embeddings`, e.g. vLLM serving bge-m3) + `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` |

The model and dimensions are stored with every version. Queries refuse a version built with a
different model, so changing the model means rebuilding.

**Optional rerank:** set `RERANK_BASE_URL` (e.g. `vllm serve BAAI/bge-reranker-v2-m3 --task
score`, which exposes `/v1/rerank`) and `RERANK_MODEL_NAME`. Vector search then returns
`KB_RERANK_CANDIDATES` chunks, and the reranker keeps the best `KB_TOP_K`. If the reranker is
down, the vector order is used.

## 4. Build, check, switch

```bash
python scripts/build_knowledge.py --dry-run          # parse + split only: check sections and chunk count
python scripts/build_knowledge.py                    # embed, write a new version, activate it
python scripts/build_knowledge.py list               # versions; which one is active
python scripts/build_knowledge.py search "备电续航下降怎么处理"
python scripts/build_knowledge.py activate <older>   # roll back instantly
python scripts/build_knowledge.py drop <version>
```

A failed build is removed and the active version is untouched. `/api/v1/health` shows the
active version, its chunk and document counts, and the rerank model.

The files in `docs/` here are a small sample about this system, not plant documentation.
Replace them with the real corpus.
