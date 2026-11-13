"""Offline knowledge-base build (scripts/build_knowledge.py): documents -> chunks -> GLM
embeddings -> new Milvus version -> self-check -> activate.

A corpus directory may contain a manifest.json:
    {"version": "2026_11_05", "title": "...",
     "documents": [{"doc_id": "dose-rate-units", "path": "docs/dose_rate_units.md",
                    "title": "...", "source": "...", "classification": "internal",
                    "access_scope": "all", "sha256": "<hex>"}]}
Each file is verified against its sha256 so only approved bytes are indexed. Without a
manifest, every .md/.txt/.pdf/.docx file is indexed and the version is a timestamp.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

from app.rag.embeddings import GLMEmbeddings
from app.rag.store import KnowledgeStore

logger = logging.getLogger(__name__)

SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx"}
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "!", "?", ";", ". ", " ", ""]


class DocEntry(BaseModel):
    doc_id: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,100}$")
    path: str
    title: str
    source: str | None = None
    classification: str = "internal"
    access_scope: str = "all"
    sha256: str | None = None


class Manifest(BaseModel):
    version: str = Field(pattern=r"^[A-Za-z0-9_]{1,60}$")
    title: str | None = None
    documents: list[DocEntry]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(corpus: Path) -> Manifest:
    path = corpus / "manifest.json"
    if path.is_file():
        return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    docs = []
    for f in sorted(p for p in corpus.rglob("*") if p.suffix.lower() in SUPPORTED):
        rel = f.relative_to(corpus).as_posix()
        docs.append(DocEntry(doc_id=re.sub(r"[^A-Za-z0-9_.\-]", "-", rel)[:100], path=rel,
                             title=f.stem, sha256=sha256(f)))
    if not docs:
        raise ValueError(f"no {sorted(SUPPORTED)} files found in {corpus}")
    return Manifest(version=datetime.now(UTC).strftime("v%Y%m%d_%H%M%S"), documents=docs)


def verify(manifest: Manifest, corpus: Path) -> dict[str, Path]:
    files, problems = {}, []
    root = corpus.resolve()
    for d in manifest.documents:
        f = (corpus / d.path).resolve()
        if root not in f.parents or not f.is_file():
            problems.append(f"{d.doc_id}: missing or outside corpus ({d.path})")
        elif d.sha256 and sha256(f) != d.sha256.lower():
            problems.append(f"{d.doc_id}: sha256 mismatch")
        else:
            files[d.doc_id] = f
    if problems:
        raise ValueError("; ".join(problems))
    return files


def docx_to_markdown(path: Path) -> str:
    """Word document -> Markdown: heading styles ("Heading 1" / "标题 1") become #, ##, ###;
    table rows become "cell | cell" lines, in document order."""
    try:
        import docx
    except ImportError as exc:
        raise RuntimeError('Word support needs: pip install -e ".[docs]"') from exc
    document = docx.Document(str(path))
    lines: list[str] = []
    for block in document.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = docx.text.paragraph.Paragraph(block, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            level = next((int(ch) for ch in style if ch.isdigit()), None) \
                if style.lower().startswith(("heading", "标题")) else None
            lines.append(f"{'#' * min(level, 3)} {text}" if level else text)
        elif tag == "tbl":
            table = docx.table.Table(block, document)
            for row in table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(dict.fromkeys(cells)))
        lines.append("")
    return "\n".join(lines)


def split(entry: DocEntry, path: Path, chunk_size: int, overlap: int) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap,
                                              separators=_SEPARATORS, keep_separator="end")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError('PDF support needs: pip install -e ".[pdf]"') from exc
        pages = [Document(page_content=p.extract_text() or "", metadata={"page": i})
                 for i, p in enumerate(PdfReader(str(path)).pages, start=1)]
    elif suffix in (".md", ".markdown", ".docx"):
        md = MarkdownHeaderTextSplitter([("#", "h1"), ("##", "h2"), ("###", "h3")],
                                        strip_headers=True)
        text = docx_to_markdown(path) if suffix == ".docx" else path.read_text(encoding="utf-8")
        pages = md.split_text(text)
    else:
        pages = [Document(page_content=path.read_text(encoding="utf-8"))]
    chunks = []
    for doc in splitter.split_documents([p for p in pages if p.page_content.strip()]):
        section = " > ".join(doc.metadata[k] for k in ("h1", "h2", "h3") if k in doc.metadata)
        chunks.append(Document(page_content=doc.page_content.strip(), metadata={
            "id": f"{entry.doc_id}#{len(chunks):05d}", "doc_id": entry.doc_id,
            "title": entry.title, "source": entry.source or "", "section": section,
            "page": doc.metadata.get("page"), "classification": entry.classification,
            "access_scope": entry.access_scope}))
    return chunks


@dataclass
class BuildResult:
    version: str
    documents: int
    chunks: int
    embedding: dict
    activated: bool
    previous_version: str | None


def build(corpus: Path, store: KnowledgeStore, embeddings: GLMEmbeddings, *,
          chunk_size: int = 800, overlap: int = 120, activate: bool = True,
          version: str | None = None) -> BuildResult:
    manifest = load_manifest(corpus)
    if version:
        manifest.version = version
    files = verify(manifest, corpus)
    chunks = [c for d in manifest.documents
              for c in split(d, files[d.doc_id], chunk_size, overlap)]
    if not chunks:
        raise ValueError("no text extracted from the corpus")
    logger.info("embedding %d chunks from %d documents", len(chunks), len(files))
    # Title + section prefix helps retrieval of short chunks; the stored text is the chunk.
    texts = [f"{c.metadata['title']} / {c.metadata['section']}\n{c.page_content}"
             if c.metadata["section"] else f"{c.metadata['title']}\n{c.page_content}"
             for c in chunks]
    vectors = embeddings.embed_documents(texts)

    meta = {"version": manifest.version, "title": manifest.title,
            "embedding": embeddings.spec, "documents": len(files), "chunks": len(chunks),
            "chunk_size": chunk_size, "chunk_overlap": overlap,
            "built_at": datetime.now(UTC).isoformat()}
    store.create(manifest.version, embeddings.dimensions, meta)
    try:
        stored = store.insert(manifest.version, [
            {**c.metadata, "text": c.page_content, "embedding": v}
            for c, v in zip(chunks, vectors, strict=True)])
        if stored != len(chunks):
            raise RuntimeError(f"stored {stored} chunks, expected {len(chunks)}")
        for i in range(0, len(chunks), max(1, len(chunks) // 5))[:5]:  # index sanity check
            hits = store.search(manifest.version, vectors[i], top_k=3)
            if chunks[i].metadata["id"] not in {h.chunk_id for h in hits}:
                raise RuntimeError(f"self-retrieval check failed for {chunks[i].metadata['id']}")
        previous = store.activate(manifest.version) if activate else store.active_version()
    except BaseException:
        store.client.drop_collection(store.collection(manifest.version))  # leave no half-build
        raise
    return BuildResult(manifest.version, len(files), len(chunks), embeddings.spec, activate,
                       previous)
