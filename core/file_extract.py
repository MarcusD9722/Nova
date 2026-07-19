from __future__ import annotations

"""Shared text extraction for .txt/.pdf/.docx (and other text-like) files.

Originally lived only in backend/app.py for chat attachment uploads; factored
out here so the local file/photo recall indexer (core/tooling.py's
memory.index_folder) can read the exact same file types without duplicating
the logic or importing from the backend layer.
"""

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".log",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".xml", ".yaml",
    ".yml", ".ini", ".toml", ".sql", ".ps1", ".sh",
}
MAX_BYTES = 200_000
MAX_CHARS = 12_000


def read_limited_bytes(path: Path, max_bytes: int = MAX_BYTES) -> tuple[bytes, bool]:
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    return data[:max_bytes], len(data) > max_bytes


def decode_text_bytes(data: bytes) -> str | None:
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("latin-1")
    except UnicodeDecodeError:
        return None


def extract_docx_text(path: Path, max_chars: int = MAX_CHARS) -> str:
    with zipfile.ZipFile(path) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    chunks = [node.text for node in root.iter() if node.tag.endswith("}t") and node.text]
    text = "\n".join(chunks).strip()
    return re.sub(r"\n{3,}", "\n\n", text)[:max_chars].rstrip()


def extract_pdf_text(path: Path, max_chars: int = MAX_CHARS) -> tuple[str | None, str | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "PDF parsing dependency is not installed"

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid PDF: {exc}"

    chunks: list[str] = []
    total_chars = 0
    for page in reader.pages:
        try:
            page_text = (page.extract_text() or "").strip()
        except Exception:
            page_text = ""
        if not page_text:
            continue
        chunks.append(page_text)
        total_chars += len(page_text)
        if total_chars >= max_chars:
            break

    if not chunks:
        return None, "PDF has no extractable text"

    text = "\n\n".join(chunks)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[truncated]"
    return text, None


def extract_excerpt(path: Path, content_type: str | None = None) -> tuple[str | None, str | None]:
    """Return (excerpt, error). Exactly one is non-None."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            text = extract_docx_text(path)
            if not text:
                return None, "document is empty"
            return text, None

        if suffix == ".pdf":
            return extract_pdf_text(path)

        if suffix in TEXT_SUFFIXES or (content_type or "").startswith("text/"):
            data, truncated = read_limited_bytes(path)
            text = decode_text_bytes(data)
            if text is None:
                return None, "could not decode file as text"
            text = text.strip()
            if not text:
                return None, "file is empty"
            if truncated or len(text) > MAX_CHARS:
                text = text[:MAX_CHARS].rstrip() + "\n[truncated]"
            return text, None
    except KeyError:
        return None, "missing document content"
    except zipfile.BadZipFile:
        return None, "invalid .docx file"
    except Exception as exc:  # noqa: BLE001
        return None, f"read failed: {exc}"

    return None, f"unsupported file type ({suffix or 'unknown'})"
