"""Document loaders -> sections with location info for citations.

Built in: Markdown (.md, heading-aware), plain text (.txt). PDF (.pdf, page-aware) needs
`pip install -e ".[pdf]"`. Add formats by registering a loader in LOADERS.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Section:
    text: str
    heading: str | None = None  # e.g. "2 剂量率单位 > 2.1 换算"
    page: int | None = None


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def load_markdown(path: Path) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, heading)
    buf: list[str] = []
    in_code = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            heading = " > ".join(title for _, title in stack) or None
            sections.append(Section(text=text, heading=heading))
        buf.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2)))
        else:
            buf.append(line)
    flush()
    return sections


def load_text(path: Path) -> list[Section]:
    text = path.read_text(encoding="utf-8").strip()
    return [Section(text=text)] if text else []


def load_pdf(path: Path) -> list[Section]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - optional extra
        raise RuntimeError('PDF support needs: pip install -e ".[pdf]"') from exc
    sections = []
    for number, page in enumerate(PdfReader(str(path)).pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            sections.append(Section(text=text, page=number))
    return sections


LOADERS: dict[str, Callable[[Path], list[Section]]] = {
    ".md": load_markdown,
    ".markdown": load_markdown,
    ".txt": load_text,
    ".pdf": load_pdf,
}


def load_document(path: Path) -> list[Section]:
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"unsupported document type {path.suffix!r}; supported: {sorted(LOADERS)}")
    return loader(path)
