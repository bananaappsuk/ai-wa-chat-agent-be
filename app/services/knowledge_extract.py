"""Extract plain text from knowledge-base uploads (txt/md/csv/pdf)."""
from __future__ import annotations

import io
from typing import Tuple


def _ext_from_name(filename: str) -> str:
    name = (filename or "").lower().rsplit(".", 1)
    return name[-1] if len(name) == 2 else ""


def extract_knowledge_text(
    *,
    data: bytes,
    filename: str,
    content_type: str | None = None,
    max_chars: int = 10000,
) -> Tuple[str, str]:
    """Return (text, source_label). Raises ValueError on unsupported/empty input."""
    if not data:
        raise ValueError("Empty file")
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("File too large (max 5 MB)")

    ct = (content_type or "").split(";")[0].strip().lower()
    ext = _ext_from_name(filename)

    is_pdf = ext == "pdf" or ct in ("application/pdf", "application/x-pdf")
    is_text = ext in ("txt", "md", "markdown", "csv") or ct.startswith("text/")

    if not is_pdf and not is_text:
        raise ValueError("Unsupported file type. Use .txt, .md, .csv, or .pdf")

    if is_pdf:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF support is not installed on the server") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            parts = [(page.extract_text() or "").strip() for page in reader.pages]
            text = "\n\n".join(p for p in parts if p).strip()
        except Exception as exc:
            raise ValueError(f"Could not read PDF: {exc}") from exc
    else:
        text = None
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("Could not decode text file")
        text = "\n".join(line.rstrip() for line in text.splitlines()).strip()

    if not text:
        raise ValueError("No readable text found in file")

    if len(text) > max_chars:
        text = text[: max_chars - 20].rstrip() + "\n…[truncated]"

    label = (filename or ("upload.pdf" if is_pdf else "upload.txt")).strip()[:120]
    return text, label
