"""Shared fakes for knowledge-base tests: deterministic embeddings, public DNS."""
import hashlib
import math
import re

DIMS = 512


def fake_vector(text: str) -> list[float]:
    """Bag-of-words hashed into DIMS buckets — texts sharing words get high cosine."""
    v = [0.0] * DIMS
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3:
            continue
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIMS] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def fake_embed(texts, **_kw):
    fake_embed.calls += 1
    fake_embed.texts.extend(texts)
    return [fake_vector(t) for t in texts]


fake_embed.calls = 0
fake_embed.texts = []


def reset_fake_embed():
    fake_embed.calls = 0
    fake_embed.texts = []


def public_dns(host, port, *a, **k):
    """socket.getaddrinfo stand-in: every host resolves to a public address."""
    return [(2, 1, 6, "", ("93.184.216.34", port or 80))]
