"""Embeddings (OpenAI text-embedding-3-small) and their compact storage format.

Vectors are stored as BSON binary vectors (subtype 9, float32) — half the size of a double
array, which matters on the free Atlas tier — and Atlas Vector Search indexes them directly.
"""
from __future__ import annotations

import math
import struct
import time
from typing import Optional, Sequence

from bson.binary import Binary

from app.config import settings

_FLOAT32 = b"\x27\x00"  # BSON binary-vector header: dtype float32, no padding
_BATCH = 96


class EmbeddingError(Exception):
    pass


def pack(vector: Sequence[float]) -> Binary:
    return Binary(_FLOAT32 + struct.pack(f"<{len(vector)}f", *vector), subtype=9)


def unpack(value) -> list[float]:
    """Accept a packed binary vector or a plain list (older/other writers)."""
    if isinstance(value, (bytes, Binary)):
        raw = bytes(value)
        if raw[:2] != _FLOAT32:
            raise ValueError("Unsupported vector encoding")
        n = (len(raw) - 2) // 4
        return list(struct.unpack(f"<{n}f", raw[2:]))
    return [float(x) for x in (value or [])]


def cosine_score(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity mapped to 0..1 the same way Atlas reports vectorSearchScore."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return (1 + dot / (na * nb)) / 2


def _record(tenant_id: Optional[str], tokens: int, latency_ms: int, ok: bool, operation: str) -> None:
    if not tenant_id:
        return
    try:
        from pymongo import MongoClient

        from app.services.ai_quota import record_usage_sync

        db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
        record_usage_sync(
            db,
            tenant_id=tenant_id,
            operation=operation,
            model=settings.KB_EMBEDDING_MODEL,
            input_tokens=tokens,
            latency_ms=latency_ms,
            success=ok,
        )
    except Exception:
        pass


def embed_texts(
    texts: list[str],
    *,
    tenant_id: Optional[str] = None,
    operation: str = "kb_embed",
    timeout: Optional[float] = None,
    attempts: int = 3,
) -> list[list[float]]:
    """Embed texts in batches. Raises EmbeddingError if the provider fails after retries.
    Reply-time lookups pass a short timeout/attempts so a slow provider can't stall a reply."""
    if not texts:
        return []
    if not (settings.OPENAI_API_KEY or "").strip():
        raise EmbeddingError("OpenAI is not configured.")
    from app.services.ai_provider import _client_get

    client = _client_get() if timeout is None else _client_get().with_options(timeout=float(timeout))
    out: list[list[float]] = []
    for start in range(0, len(texts), _BATCH):
        batch = [t[:8000] or " " for t in texts[start:start + _BATCH]]
        last: Optional[Exception] = None
        for attempt in range(max(1, attempts)):
            t0 = time.monotonic()
            try:
                resp = client.embeddings.create(
                    model=settings.KB_EMBEDDING_MODEL,
                    input=batch,
                    dimensions=int(settings.KB_EMBEDDING_DIMS),
                )
                latency = int((time.monotonic() - t0) * 1000)
                _record(tenant_id, int(getattr(resp.usage, "total_tokens", 0) or 0), latency, True, operation)
                out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
                last = None
                break
            except Exception as exc:  # network / rate limit / provider error
                last = exc
                if attempt + 1 < max(1, attempts):
                    time.sleep(1.5 * (attempt + 1))
        if last is not None:
            _record(tenant_id, 0, 0, False, operation)
            raise EmbeddingError(f"Embedding failed ({type(last).__name__}).") from last
    return out
