"""Site icons for cited sources, fetched by the server rather than the page.

A citation reads better with the source's mark next to it, and the obvious
way to get one is an `<img>` pointing at the site — or at a favicon service.
Both are wrong here. The browser would then announce, to every site in a
list of sources and to whoever runs the service, which sources somebody is
reading: the answer is on screen, nobody has clicked anything, and the
reading is private until they do. Compass already fetches the web on the
server's side of the wire, so the icons come the same way.

Everything is cached on disk by host. Icons change about once a year, the
same handful of hosts recur across a conversation, and a cache miss is the
only time anything leaves this machine.

Failure is ordinary and expected. Plenty of sites have no icon, serve one
the browser cannot read, or are simply slow. The answer then is a 404 and a
lettered monogram in the page — never a broken image, and never a blocked
render while a third party decides whether to answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from compass.common.config import get_settings
from compass.common.tools.web_fetch import _unsafe

logger = logging.getLogger("compass.home")

#: Small by nature. Anything larger is a logo sheet or a mistake, and either
#: way it is not going in a 16-pixel box.
MAX_BYTES = 256_000

#: How long a cached icon is trusted. Long, because these change rarely and a
#: stale mark is a smaller cost than a request per conversation.
TTL_SECONDS = 30 * 24 * 3600

#: What a browser will actually render in an <img>. SVG is deliberately not
#: served back: it is a document that can carry script, and the sanitiser
#: that would make it safe is not worth writing for a 16-pixel decoration.
_OK_TYPES = {
    "image/png": ".png", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
}

#: `<link rel="icon" href="...">`, in any of the spellings sites use.
_LINK_RE = re.compile(
    rb"""<link[^>]+rel=["']?[^"'>]*\bicon\b[^"'>]*["']?[^>]*>""", re.I)
_HREF_RE = re.compile(rb"""href=["']([^"'>]+)["']""", re.I)

_locks: dict[str, asyncio.Lock] = {}


def host_of(url: str) -> str:
    """The host an icon would belong to, or "" when there is not one."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host if "." in host else ""


def _dir() -> Path:
    settings = get_settings()
    folder = settings.workspace_root / settings.data_dir / "favicons"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _cached(host: str) -> Path | None:
    """The freshest usable file for this host, or None.

    A zero-byte file is a remembered failure: it stops a site with no icon
    being asked again on every message.
    """
    stem = hashlib.sha256(host.encode()).hexdigest()[:20]
    for path in _dir().glob(f"{stem}.*"):
        if time.time() - path.stat().st_mtime > TTL_SECONDS:
            path.unlink(missing_ok=True)
            return None
        return path
    return None


async def _download(client, url: str) -> tuple[bytes, str] | None:
    if _unsafe(url):
        return None
    try:
        res = await client.get(url, timeout=6.0, follow_redirects=True)
    except Exception:  # noqa: BLE001 — a missing icon is not an error
        return None
    if res.status_code != 200 or len(res.content) > MAX_BYTES or not res.content:
        return None
    kind = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
    if kind not in _OK_TYPES:
        return None
    return res.content, _OK_TYPES[kind]


async def fetch(host: str) -> Path | None:
    """The icon file for a host, from cache or from the site. None if there
    is not one.

    Two places are tried, in the order a browser would: whatever the home
    page declares with `<link rel="icon">`, then `/favicon.ico`. The declared
    one first because a site that bothers to declare one usually has the
    better image at a path of its own choosing.
    """
    if not host or _unsafe(f"https://{host}/"):
        return None

    hit = _cached(host)
    if hit is not None:
        return hit if hit.stat().st_size else None

    lock = _locks.setdefault(host, asyncio.Lock())
    async with lock:
        # Another request may have fetched it while this one waited.
        hit = _cached(host)
        if hit is not None:
            return hit if hit.stat().st_size else None

        import httpx

        found: tuple[bytes, str] | None = None
        async with httpx.AsyncClient(
            headers={"user-agent": "Mozilla/5.0 (compatible; Compass/1.0)"},
        ) as client:
            page_url = f"https://{host}/"
            try:
                page = await client.get(page_url, timeout=6.0, follow_redirects=True)
                for tag in _LINK_RE.findall(page.content[:200_000]):
                    href = _HREF_RE.search(tag)
                    if not href:
                        continue
                    target = urljoin(str(page.url), href.group(1).decode("utf-8", "ignore"))
                    if found := await _download(client, target):
                        break
            except Exception:  # noqa: BLE001 — fall through to /favicon.ico
                pass
            if not found:
                found = await _download(client, f"{page_url}favicon.ico")

        stem = hashlib.sha256(host.encode()).hexdigest()[:20]
        if not found:
            # Remembered as a miss, so the next message does not try again.
            (_dir() / f"{stem}.none").write_bytes(b"")
            return None
        body, ext = found
        path = _dir() / f"{stem}{ext}"
        path.write_bytes(body)
        logger.info("cached a site icon for %s (%d bytes)", host, len(body))
        return path
