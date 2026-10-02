"""Website crawler for the knowledge base (server-rendered HTML; no JavaScript execution).

Scope: same site as the start URL (www. ignored), under the include path prefixes (default:
the start URL's folder), minus exclude patterns. Seeds from robots.txt sitemaps and
/sitemap.xml, then follows links breadth-first up to max_depth. Obeys robots.txt (RFC 9309),
waits between requests, and stops at max_pages or the time budget.
"""
from __future__ import annotations

import fnmatch
import logging
import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.services.kb.extract import MIN_PAGE_CHARS, ExtractError, Extracted, extract_response
from app.services.kb.fetch import FetchError, fetch

logger = logging.getLogger(__name__)

_SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|css|js|json|xml|rss|atom|zip|gz|tar|rar|7z|"
    r"mp3|mp4|m4a|wav|avi|mov|webm|woff2?|ttf|eot|otf|exe|dmg|apk|iso|docx?|xlsx?|pptx?|csv)$",
    re.I,
)
_TRACKING = re.compile(r"^(utm_\w+|gclid|fbclid|mc_cid|mc_eid|ref|_ga|_gl|igshid)$", re.I)


@dataclass
class CrawlConfig:
    start_url: str
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    max_pages: int = 50
    max_depth: int = 3


@dataclass
class CrawledPage:
    url: str
    page: Extracted


_FRONTIER_CAP = 3000
_SEEN_CAP = 6000


@dataclass
class CrawlState:
    """Resumable crawl progress, persisted between job slices so a long crawl never holds
    the (single) worker for minutes — AI replies get processed between slices."""

    frontier: list = field(default_factory=list)  # [[url, depth], ...]
    seen: list = field(default_factory=list)
    seeded: bool = False
    fetched: int = 0
    skipped_robots: int = 0
    errors: int = 0
    thin: int = 0
    elapsed: float = 0.0
    done: bool = False
    stopped_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "frontier": self.frontier[:_FRONTIER_CAP],
            "seen": self.seen[-_SEEN_CAP:],
            "seeded": self.seeded,
            "fetched": self.fetched,
            "skipped_robots": self.skipped_robots,
            "errors": self.errors,
            "thin": self.thin,
            "elapsed": round(self.elapsed, 2),
            "done": self.done,
            "stopped_reason": self.stopped_reason,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "CrawlState":
        d = d or {}
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def stats(self) -> dict:
        return {k: v for k, v in self.to_dict().items() if k not in ("frontier", "seen")}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if not port or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)))
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((scheme, netloc, path, query, ""))


def _site(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def default_include_path(start_url: str) -> str:
    path = urlsplit(start_url).path or "/"
    return path if path.endswith("/") else (path.rsplit("/", 1)[0] + "/")


def in_scope(url: str, cfg: CrawlConfig) -> bool:
    parts, start = urlsplit(url), urlsplit(cfg.start_url)
    if parts.scheme not in ("http", "https") or _site(parts.hostname or "") != _site(start.hostname or ""):
        return False
    path = parts.path or "/"
    if _SKIP_EXT.search(path):
        return False
    includes = cfg.include_paths or [default_include_path(cfg.start_url)]
    if not any(path.startswith(p if p.startswith("/") else "/" + p) for p in includes):
        return False
    for pattern in cfg.exclude_paths:
        pat = pattern if pattern.startswith("/") else "/" + pattern
        if path.startswith(pat.rstrip("*")) and "*" not in pat or fnmatch.fnmatch(path, pat):
            return False
    return True


class _Robots:
    def __init__(self, client: httpx.Client):
        self.client = client
        self.parsers: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.sitemaps: dict[str, list[str]] = {}

    def _load(self, origin: str) -> urllib.robotparser.RobotFileParser:
        if origin in self.parsers:
            return self.parsers[origin]
        rp = urllib.robotparser.RobotFileParser()
        try:
            res = fetch(f"{origin}/robots.txt", client=self.client, max_bytes=500_000)
            if res.status >= 500:
                rp.parse(["User-agent: *", "Disallow: /"])  # RFC 9309: server error → assume disallow
            elif res.status >= 400:
                rp.parse([])  # unavailable → allow all
            else:
                lines = res.text.splitlines()
                rp.parse(lines)
                self.sitemaps[origin] = [
                    ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")
                ]
        except FetchError:
            rp.parse([])
        self.parsers[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        return self._load(f"{p.scheme}://{p.netloc}").can_fetch(settings.KB_USER_AGENT, url)

    def crawl_delay(self, url: str) -> float:
        p = urlsplit(url)
        delay = self._load(f"{p.scheme}://{p.netloc}").crawl_delay(settings.KB_USER_AGENT)
        return min(5.0, float(delay)) if delay else 0.0


def _sitemap_urls(client: httpx.Client, robots: _Robots, cfg: CrawlConfig, cap: int) -> list[str]:
    p = urlsplit(cfg.start_url)
    origin = f"{p.scheme}://{p.netloc}"
    robots._load(origin)
    pending = list(dict.fromkeys(robots.sitemaps.get(origin, []) + [f"{origin}/sitemap.xml"]))
    seen_maps: set[str] = set()
    urls: list[str] = []
    while pending and len(seen_maps) < 10 and len(urls) < cap:
        sm = pending.pop(0)
        if sm in seen_maps or sm.endswith(".gz"):
            continue
        seen_maps.add(sm)
        try:
            res = fetch(sm, client=client, max_bytes=5_000_000)
        except FetchError:
            continue
        if res.status != 200:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", res.text, re.I)
        if "<sitemapindex" in res.text.lower():
            pending.extend(locs)
            continue
        for loc in locs:
            u = normalize_url(loc)
            if in_scope(u, cfg):
                urls.append(u)
                if len(urls) >= cap:
                    break
    return urls


def crawl_slice(
    cfg: CrawlConfig,
    state: CrawlState,
    *,
    budget_seconds: float,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[CrawledPage]:
    """Crawl for up to `budget_seconds`, yielding extracted pages and updating `state`.
    Call again with the same state to continue; `state.done` is set when finished.
    Never raises for individual page failures."""
    max_pages = max(1, min(int(cfg.max_pages or settings.KB_CRAWL_DEFAULT_MAX_PAGES), int(settings.KB_CRAWL_MAX_PAGES)))
    max_depth = max(0, min(int(cfg.max_depth), int(settings.KB_CRAWL_MAX_DEPTH)))
    slice_end = time.monotonic() + max(1.0, float(budget_seconds))
    slice_start = time.monotonic()
    own = client is None
    http = client or httpx.Client(
        timeout=float(settings.KB_FETCH_TIMEOUT_SECONDS),
        headers={"User-Agent": settings.KB_USER_AGENT},
        follow_redirects=False,
    )
    seen: set[str] = set(state.seen)
    queue: deque = deque(tuple(x) for x in state.frontier)

    def _visit(u: str, depth: int) -> None:
        if u not in seen and len(queue) < _FRONTIER_CAP:
            seen.add(u)
            state.seen.append(u)
            queue.append((u, depth))

    try:
        robots = _Robots(http)
        if not state.seeded:
            _visit(normalize_url(cfg.start_url), 0)
            for u in _sitemap_urls(http, robots, cfg, cap=max_pages * 3):
                _visit(u, 1)
            state.seeded = True
        fetched_this_slice = 0
        while queue:
            if state.fetched >= max_pages:
                state.stopped_reason = "max_pages"
                break
            if state.elapsed + (time.monotonic() - slice_start) > int(settings.KB_CRAWL_TIME_BUDGET_SECONDS):
                state.stopped_reason = "time_budget"
                break
            if fetched_this_slice and time.monotonic() > slice_end:
                break  # slice over — resume in the next job
            url, depth = queue.popleft()
            if not robots.allowed(url):
                state.skipped_robots += 1
                continue
            if fetched_this_slice:
                sleep(max(float(settings.KB_CRAWL_DELAY_SECONDS), robots.crawl_delay(url)))
            fetched_this_slice += 1
            try:
                res = fetch(url, client=http)
            except FetchError as exc:
                state.errors += 1
                logger.info("kb crawl fetch failed url=%s err=%s", url, exc)
                continue
            final = normalize_url(res.url)
            if res.status != 200 or (final != url and not in_scope(final, cfg)):
                state.errors += 1
                continue
            if final != url:
                seen.add(final)
                state.seen.append(final)
            try:
                page = extract_response(res.body, res.content_type, final)
            except ExtractError:
                state.errors += 1
                continue
            state.fetched += 1
            if depth < max_depth:
                for link in page.links:
                    n = normalize_url(link)
                    if in_scope(n, cfg):
                        _visit(n, depth + 1)
            if len(page.text) < MIN_PAGE_CHARS:
                state.thin += 1
                continue
            yield CrawledPage(url=final, page=page)
        if not queue and not state.stopped_reason:
            state.stopped_reason = "complete"
        state.done = bool(state.stopped_reason)
    finally:
        state.frontier = [list(x) for x in queue]
        state.elapsed += time.monotonic() - slice_start
        if own:
            http.close()
