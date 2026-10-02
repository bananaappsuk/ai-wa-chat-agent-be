"""Outbound HTTP for the knowledge base, hardened against SSRF.

Tenants supply URLs, so every request (and every redirect hop) must resolve only to public
addresses — never localhost, private ranges, link-local/cloud-metadata or other reserved IPs.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5


class FetchError(Exception):
    """A URL we refuse to fetch, or a fetch that failed. Message is safe to show the tenant."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        charset = "utf-8"
        for part in self.content_type.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"')
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


# IPv6 forms that carry an IPv4 address inside (NAT64 per RFC 6052/8215) — judge the
# embedded IPv4, so a public site behind DNS64 is allowed but 64:ff9b::7f00:1 (127.0.0.1) isn't.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif addr.sixtofour:
            addr = addr.sixtofour
        elif addr.teredo:
            addr = addr.teredo[1]
        elif any(addr in net for net in _NAT64):
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> str:
    """Validate scheme + host and that the host resolves only to public IPs. Returns the URL."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError("Only http and https URLs are supported.")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise FetchError("The URL has no host.")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise FetchError("That address isn't allowed.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise FetchError(f"Couldn't resolve {host}.") from exc
    ips = {info[4][0] for info in infos}
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise FetchError("That address isn't allowed.")
    return url


def fetch(
    url: str,
    *,
    client: Optional[httpx.Client] = None,
    max_bytes: Optional[int] = None,
    accept: str = "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
) -> FetchResult:
    """GET a public URL, following redirects manually so each hop is re-validated."""
    limit = int(max_bytes or settings.KB_FETCH_MAX_BYTES)
    own = client is None
    http = client or httpx.Client(
        timeout=float(settings.KB_FETCH_TIMEOUT_SECONDS),
        headers={"User-Agent": settings.KB_USER_AGENT, "Accept": accept},
        follow_redirects=False,
    )
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            assert_public_url(current)
            with http.stream("GET", current) as resp:
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise FetchError("Redirect without a location.")
                    current = urljoin(current, loc)
                    continue
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise FetchError(f"The page is larger than {limit // 1_000_000} MB.")
                return FetchResult(
                    url=str(resp.url),
                    status=resp.status_code,
                    content_type=(resp.headers.get("content-type") or "").lower(),
                    body=bytes(body),
                )
        raise FetchError("Too many redirects.")
    except httpx.HTTPError as exc:
        raise FetchError(f"Couldn't fetch the page ({type(exc).__name__}).") from exc
    finally:
        if own:
            http.close()
