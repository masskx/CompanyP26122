"""Knowledge release manifest: the fixed, versioned corpus definition (FR-KB-01/02).

    {
      "version": "2026_11_05_01",           # letters, digits, underscore (Milvus naming)
      "title": "...",
      "documents": [
        {"doc_id": "dose-rate-units", "path": "docs/dose_rate_units.md",
         "title": "...", "source": "...", "doc_version": "1.0",
         "effective_date": "2026-11-01", "classification": "internal",
         "access_scope": "all", "sha256": "<hex>"}
      ]
    }

Paths are relative to the manifest file. Every file is checked against its SHA-256 before
anything is embedded, so only the approved bytes can enter the index.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManifestError(ValueError):
    pass


class DocumentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,100}$")
    path: str
    title: str = Field(min_length=1, max_length=300)
    source: str | None = Field(default=None, max_length=300)
    doc_version: str | None = Field(default=None, max_length=60)
    effective_date: date | None = None
    classification: str = Field(default="internal", pattern=r"^[A-Za-z0-9_\-]{1,30}$")
    # "all", or a role that may see this document (matched against the user's roles).
    access_scope: str = Field(default="all", pattern=r"^[A-Za-z0-9_\-]{1,60}$")
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class KnowledgeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[A-Za-z0-9_]{1,60}$")
    title: str | None = None
    documents: list[DocumentEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "KnowledgeManifest":
        ids = [d.doc_id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate doc_id in manifest")
        return self


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path) -> tuple[KnowledgeManifest, str]:
    """Return the validated manifest and the SHA-256 of the manifest file itself."""
    raw = path.read_bytes()
    manifest = KnowledgeManifest.model_validate(json.loads(raw))
    return manifest, hashlib.sha256(raw).hexdigest()


def verify_documents(manifest: KnowledgeManifest, base_dir: Path) -> dict[str, Path]:
    """Resolve and checksum every document. Raises ManifestError listing all problems."""
    resolved: dict[str, Path] = {}
    problems: list[str] = []
    root = base_dir.resolve()
    for doc in manifest.documents:
        path = (base_dir / doc.path).resolve()
        if root not in path.parents:
            problems.append(f"{doc.doc_id}: path escapes the corpus directory")
            continue
        if not path.is_file():
            problems.append(f"{doc.doc_id}: file not found ({doc.path})")
            continue
        actual = sha256_file(path)
        if actual.lower() != doc.sha256.lower():
            problems.append(f"{doc.doc_id}: sha256 mismatch (manifest {doc.sha256[:12]}…, "
                            f"file {actual[:12]}…)")
            continue
        resolved[doc.doc_id] = path
    if problems:
        raise ManifestError("; ".join(problems))
    return resolved
