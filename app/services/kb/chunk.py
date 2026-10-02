"""Heading-aware chunking.

Text is split into sections by Markdown headings, then packed paragraph by paragraph into
chunks of ~KB_CHUNK_TARGET_CHARS (never more than KB_CHUNK_MAX_CHARS). Consecutive chunks of
the same section overlap by ~KB_CHUNK_OVERLAP_CHARS so facts spanning a boundary aren't lost.
Each chunk remembers its heading path, which is prepended when embedding ("contextual"
chunks retrieve far better than bare paragraphs).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.config import settings

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Chunk:
    index: int
    heading: str
    text: str


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Split an over-long paragraph on sentence boundaries, hard-splitting as a last resort."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    parts: list[str] = []
    buf = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if buf and len(buf) + 1 + len(sentence) > max_chars:
            parts.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf)
    return parts


def _tail(text: str, size: int) -> str:
    """Last ~size chars of text, starting at a sentence or word boundary."""
    if size <= 0 or len(text) <= size:
        return text if size > 0 else ""
    tail = text[-size:]
    m = _SENTENCE_END.search(tail)
    if m and m.end() < len(tail):
        return tail[m.end():]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def _sections(text: str) -> list[tuple[str, list[str]]]:
    path: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = [("", [])]
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        m = _HEADING.match(lines[0])
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
            sections.append((" > ".join(t for _, t in path), []))
            rest = "\n".join(lines[1:]).strip()
            if rest:
                sections[-1][1].append(rest)
        else:
            sections[-1][1].append(block)
    return [(h, paras) for h, paras in sections if paras]


def chunk_text(
    text: str,
    *,
    target: Optional[int] = None,
    max_chars: Optional[int] = None,
    overlap: Optional[int] = None,
) -> list[Chunk]:
    target = int(target or settings.KB_CHUNK_TARGET_CHARS)
    max_chars = max(target, int(max_chars or settings.KB_CHUNK_MAX_CHARS))
    overlap = max(0, min(int(overlap if overlap is not None else settings.KB_CHUNK_OVERLAP_CHARS), target // 2))

    chunks: list[Chunk] = []
    for heading, paragraphs in _sections(text or ""):
        pieces: list[str] = []
        for p in paragraphs:
            pieces.extend(_split_long(p, max_chars))
        buf = ""
        for piece in pieces:
            candidate = f"{buf}\n\n{piece}" if buf else piece
            if buf and len(candidate) > target:
                chunks.append(Chunk(index=len(chunks), heading=heading, text=buf))
                carry = _tail(buf, overlap)
                buf = f"{carry}\n\n{piece}" if carry and len(carry) + len(piece) + 2 <= max_chars else piece
            else:
                buf = candidate
        if buf.strip():
            chunks.append(Chunk(index=len(chunks), heading=heading, text=buf))
    return _merge_small(chunks, target, max_chars)


def _merge_small(chunks: list[Chunk], target: int, max_chars: int) -> list[Chunk]:
    """Pages built from many tiny heading sections produce scraps (a heading, a one-line
    card) that waste retrieval slots and carry no context. Fold a small chunk into its
    neighbour (keeping its heading inline) while the result stays within max_chars. Short
    but real content is never thrown away — only symbol-only scraps like "01" or "→"."""
    min_chars = max(200, target // 3)
    out: list[Chunk] = []
    for c in chunks:
        if out:
            prev = out[-1]
            small = len(prev.text) < min_chars or len(c.text) < min_chars
            body = c.text if c.heading == prev.heading or not c.heading else f"{c.heading}\n{c.text}"
            if small and len(prev.text) + len(body) + 2 <= max_chars:
                out[-1] = Chunk(index=prev.index, heading=prev.heading or c.heading, text=f"{prev.text}\n\n{body}")
                continue
        out.append(c)
    if len(out) > 1:
        out = [c for c in out if len(re.sub(r"\W", "", c.text)) >= 3] or out
    return [Chunk(index=i, heading=c.heading, text=c.text) for i, c in enumerate(out)]


def embedding_input(doc_title: str, chunk: Chunk) -> str:
    head = " — ".join(x for x in (doc_title.strip(), chunk.heading.strip()) if x)
    return f"{head}\n\n{chunk.text}" if head else chunk.text
