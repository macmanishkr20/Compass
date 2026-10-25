"""Which URLs a browser may be pointed at.

A page the agent has just read can contain instructions written by whoever
controls that page, and the agent may act on them — that is the whole shape of
an indirect prompt injection. The dangerous version is not "it visits a page
you did not expect": it is a scheme that is not the web at all. `file:///etc/
passwd` turns a browser into a file reader with no permission gate in front of
it. `javascript:` runs script in whatever page is already open, with that
page's origin and session. `data:` renders attacker-authored markup as a
first-class page.

So the rule is the narrow one: a navigation is http or https, and nothing
else. This is the same guidance the browser-use documentation gives, and it is
deliberately a scheme check rather than a host check — Compass browses
localhost dev servers on purpose, and an allowlist of hosts would break that
without closing the hole that matters.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The only two schemes a page may be opened with.
WEB_SCHEMES = ("http", "https")

#: Named so the refusal can say what was wrong rather than only that it was.
_WHY = {
    "file": "reads local files rather than a web page",
    "javascript": "runs script inside whatever page is already open",
    "data": "renders content supplied in the URL itself as a page",
    "blob": "opens an in-memory object rather than a web page",
    "vbscript": "runs script inside whatever page is already open",
    "view-source": "reads a page's source through the browser's own viewer",
    "chrome": "reaches the browser's internal pages",
    "chrome-extension": "reaches an installed extension",
    "about": "reaches the browser's internal pages",
}


def refuse_reason(raw: str) -> str:
    """Why this URL may not be opened, or "" when it may.

    Returns a sentence rather than a boolean because the answer goes back to
    the model as a tool result, and a tool result that says only "failed"
    teaches it nothing about what to do next.
    """
    url = (raw or "").strip()
    if not url:
        return "no URL was given"
    scheme = urlsplit(url).scheme.lower()
    if scheme in WEB_SCHEMES:
        return ""
    if not scheme:
        # No scheme at all is not a refusal here: what happens next is a
        # navigation failure from the browser, which is the caller's business.
        return ""
    why = _WHY.get(scheme, "is not a web page")
    return (
        f"refusing to open a {scheme}: URL — it {why}. "
        "Only http:// and https:// addresses can be opened."
    )
