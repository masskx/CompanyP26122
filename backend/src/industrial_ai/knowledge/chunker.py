"""Sentence-aware chunking for Chinese/English technical text.

Chunks are packed from sentences/paragraphs up to `max_chars`; neighbouring chunks share
up to `overlap` characters of trailing sentences so context is not cut mid-thought.
The embedded text is prefixed with the document title and section heading, which helps
retrieval for short chunks; the stored text is the chunk itself.
"""

import re
from dataclasses import dataclass

from industrial_ai.knowledge.loaders import Section
from industrial_ai.knowledge.manifest import DocumentEntry

CHUNKER_VERSION = 1

_UNIT_SPLIT = re.compile(r"(?<=[。！？；!?;])|(?<=[.](?=\s))|\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    chunk_index: int
    text: str
    title: str
    source: str | None
    section: str | None
    page: int | None
    doc_version: str | None
    classification: str
    access_scope: str

    def embedding_text(self) -> str:
        header = self.title if not self.section else f"{self.title} / {self.section}"
        return f"{header}\n{self.text}"


def _units(text: str, max_chars: int) -> list[str]:
    units = []
    for piece in _UNIT_SPLIT.split(text):
        piece = piece.strip()
        while len(piece) > max_chars:  # hard-split very long sentences/tables
            units.append(piece[:max_chars])
            piece = piece[max_chars:]
        if piece:
            units.append(piece)
    return units


def split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in _units(text, max_chars):
        if current and size + len(unit) > max_chars:
            chunks.append("".join(current) if _is_cjk(current) else " ".join(current))
            tail: list[str] = []
            tail_size = 0
            for prev in reversed(current):
                if tail_size + len(prev) > overlap:
                    break
                tail.insert(0, prev)
                tail_size += len(prev)
            current, size = tail, tail_size
        current.append(unit)
        size += len(unit)
    if current:
        chunks.append("".join(current) if _is_cjk(current) else " ".join(current))
    return chunks


def _is_cjk(units: list[str]) -> bool:
    sample = "".join(units)[:200]
    return sum("一" <= ch <= "鿿" for ch in sample) > len(sample) * 0.2


def chunk_document(doc: DocumentEntry, sections: list[Section], *, max_chars: int,
                   overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in sections:
        for text in split_text(section.text, max_chars, overlap):
            chunks.append(Chunk(
                chunk_id=f"{doc.doc_id}#{len(chunks):05d}", doc_id=doc.doc_id,
                chunk_index=len(chunks), text=text, title=doc.title, source=doc.source,
                section=section.heading, page=section.page, doc_version=doc.doc_version,
                classification=doc.classification, access_scope=doc.access_scope,
            ))
    return chunks
