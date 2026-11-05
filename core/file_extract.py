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

#: The line that separates what the USER typed from the contents of the files
#: they attached. Defined here, in the module that owns attachment extraction,
#: because two places need to agree on it: `backend/app.py` writes it, and
#: `core/brain.py` has to know where the user's prose stops before it starts
#: rewriting whitespace and role prefixes. Guessing the boundary is how a
#: compose file containing `user: admin` became the entire user message
#: (audit 2026-11-02, H1).
ATTACHMENT_HEADER = "Attached file context:"


def read_limited_bytes(path: Path, max_bytes: int = MAX_BYTES) -> tuple[bytes, bool]:
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    return data[:max_bytes], len(data) > max_bytes


#: UTF-16 byte-order marks. UTF-16 is only tried when one is present.
_UTF16_BOMS = ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


def decode_text_bytes(data: bytes) -> str | None:
    """Decode a text file's bytes, or None if nothing plausible fits.

    utf-8-sig FIRST: it strips a BOM when present and is otherwise identical to
    utf-8. Ordered the other way round, plain utf-8 matched first and left the
    BOM in place, so every BOM'd file (the Windows default for a lot of
    editors) carried an invisible ﻿ into chat attachments, indexed document
    chunks, and the embeddings built from them.

    UTF-16 ONLY WITH A BOM (audit, M13). Bare "utf-16-le"/"utf-16-be" decode
    almost any even-length byte string without error, so a cp1252 file with one
    non-ASCII byte — a smart quote, an accented name, a degree sign — fell
    through utf-8 and came out as fluent-looking CJK. Silently: no exception,
    no warning, straight into chat and into indexed memory. A BOM is the only
    positive evidence that a file really is UTF-16, so without one the
    single-byte fallback is used, which is what such a file almost always is.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    for bom, encoding in _UTF16_BOMS:
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                break
    # cp1252 before latin-1: it is the actual Windows default, and it maps the
    # 0x80-0x9F range (smart quotes, em dash, euro) that latin-1 turns into
    # control characters. Undefined bytes there fall through to latin-1, which
    # decodes anything.
    for encoding in ("cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
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
