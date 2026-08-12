"""Recap — "how you've been working with Compass" (the port of Claude's
Settings → Reflect monthly recap).

Everything here is derived from data Compass already keeps — agent session
metadata and Home chat cards — so no extra tracking is introduced: how many
conversations, which topics came up, the most active day, the peak hour, and a
couple of plain-language observations.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Words too common to be a "topic".
_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "then", "this", "that", "these",
    "those", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on",
    "for", "with", "from", "by", "at", "as", "it", "its", "my", "me", "i",
    "you", "your", "we", "our", "us", "can", "could", "should", "would", "will",
    "do", "does", "did", "how", "what", "why", "when", "where", "which", "who",
    "please", "show", "make", "give", "tell", "use", "using", "used", "new",
    "get", "got", "run", "add", "fix", "check", "help", "want", "need", "like",
    "same", "not", "no", "yes", "all", "any", "some", "more", "most", "very",
    "one", "two", "also", "just", "only", "now", "here", "there", "so", "up",
    "out", "about", "into", "over", "than", "them", "they", "he", "she",
    # Common instruction verbs — they describe the ask, not the subject.
    "create", "change", "generate", "build", "open", "start", "set", "work",
    "working", "update", "explain", "find", "list", "read", "see", "try",
    "correct", "compare", "checked", "done", "exactly", "again",
}


def _topics(titles: list[str], limit: int = 6) -> list[dict]:
    counts: Counter[str] = Counter()
    for t in titles:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{2,}", (t or "").lower()):
            if w in _STOP or len(w) < 3:
                continue
            counts[w] += 1
    return [
        {"topic": w, "count": c}
        for w, c in counts.most_common(limit)
        if c > 1 or len(counts) <= limit
    ]


def _hour_label(h: int) -> str:
    suffix = "am" if h < 12 else "pm"
    hr = h % 12 or 12
    return f"{hr}{suffix}"


async def build_recap(days: int = 30) -> dict:
    """Aggregate the last `days` of activity into a recap payload."""
    since = time.time() - days * 86400

    stamps: list[float] = []
    titles: list[str] = []
    agent_n = chat_n = 0

    # Agent conversations.
    try:
        from compass.persistence.session_meta import get_meta_store

        for m in await get_meta_store().list_all():
            ts = getattr(m, "updated_at", 0) or getattr(m, "created_at", 0) or 0
            if ts >= since:
                stamps.append(ts)
                titles.append(getattr(m, "title", "") or "")
                agent_n += 1
    except Exception:  # noqa: BLE001 — a recap must never break the app
        pass

    # Home chats.
    try:
        from compass.core.chat_engine import get_chat_store

        for c in await get_chat_store().list_cards():
            ts = c.get("updated_at", 0)
            if ts >= since:
                stamps.append(ts)
                titles.append(c.get("title", "") or "")
                chat_n += 1
    except Exception:  # noqa: BLE001
        pass

    by_day: Counter[str] = Counter()
    by_hour: Counter[int] = Counter()
    for ts in stamps:
        d = datetime.fromtimestamp(ts)
        by_day[_DAYS[d.weekday()]] += 1
        by_hour[d.hour] += 1

    top_day = by_day.most_common(1)[0] if by_day else None
    top_hour = by_hour.most_common(1)[0] if by_hour else None
    topics = _topics(titles)

    observations: list[str] = []
    total = agent_n + chat_n
    if total:
        if agent_n and chat_n:
            leaning = "building" if agent_n > chat_n else "thinking things through"
            observations.append(
                f"You split time between Code and Home, leaning towards {leaning}."
            )
        elif agent_n:
            observations.append("Almost all of your time was spent in Code, building.")
        else:
            observations.append("You mostly used Home to think and talk things through.")
    if top_day:
        observations.append(f"{top_day[0]} was your busiest day.")
    if top_hour is not None:
        observations.append(f"You work most around {_hour_label(top_hour[0])}.")
    if topics:
        observations.append(
            "Recurring themes: " + ", ".join(t["topic"] for t in topics[:3]) + "."
        )

    return {
        "days": days,
        "conversations": total,
        "agent_conversations": agent_n,
        "chat_conversations": chat_n,
        "top_day": {"day": top_day[0], "count": top_day[1]} if top_day else None,
        "peak_hour": (
            {"hour": top_hour[0], "label": _hour_label(top_hour[0]), "count": top_hour[1]}
            if top_hour is not None
            else None
        ),
        "by_day": [{"day": d, "count": by_day.get(d, 0)} for d in _DAYS],
        "topics": topics,
        "observations": observations,
    }
