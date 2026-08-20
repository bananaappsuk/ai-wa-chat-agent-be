"""SSRF protections for outbound HTTP fetches (media proxy, etc.)."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

# Twilio API / media hosts (plus common CDN patterns used by Twilio)
_ALLOWED_REMOTE_HOST_SUFFIXES = (
    "twilio.com",
    "twiliousercontent.com",
    "twiliocdn.com",
)

_ALLOWED_META_MEDIA_HOST_SUFFIXES = (
    "fbcdn.net",
    "facebook.com",
    "fbsbx.com",
    "whatsapp.net",
)


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_safe_remote_media_url(url: str) -> str:
    """Allow only https Twilio media hosts; block private/metadata targets."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise HTTPException(status_code=400, detail="Remote media URL must be https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="Invalid media URL")
    if host in ("localhost", "metadata.google.internal") or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Remote media host not allowed")
    if host.startswith("169.254.") or host == "169.254.169.254":
        raise HTTPException(status_code=400, detail="Remote media host not allowed")

    allowed = any(host == s or host.endswith("." + s) for s in _ALLOWED_REMOTE_HOST_SUFFIXES)
    if not allowed:
        raise HTTPException(status_code=400, detail="Remote media host not allowed")

    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail="Remote media host not resolvable") from exc

    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise HTTPException(status_code=400, detail="Remote media host resolves to private IP")

    return raw


def assert_safe_meta_media_url(url: str) -> str:
    """Allow only https Meta/Facebook media hosts; block private/metadata targets.

    Raises ValueError (not HTTPException) so webhook handlers can fail closed with HTTP 200.
    """
    raw = (url or "").strip()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise ValueError("Remote media URL must be https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Invalid media URL")
    if host in ("localhost", "metadata.google.internal") or host.endswith(".local"):
        raise ValueError("Remote media host not allowed")
    if host.startswith("169.254.") or host == "169.254.169.254":
        raise ValueError("Remote media host not allowed")
    allowed = any(host == s or host.endswith("." + s) for s in _ALLOWED_META_MEDIA_HOST_SUFFIXES)
    if not allowed:
        raise ValueError("Remote media host not allowed")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Remote media host not resolvable") from exc
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise ValueError("Remote media host resolves to private IP")
    return raw
