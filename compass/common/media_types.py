"""What to tell a browser a file is.

Shared by every section that hands a file back, which is why it lives here
rather than inside one of them.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path


#: What to tell the browser a file is, by extension.
#:
#: Explicit, because `mimetypes.guess_type` is not a fixed table: on Windows
#: it reads the answers out of the registry, so the type a file is served as
#: depends on what is installed on the machine running Compass rather than on
#: the file. When the registry disagrees, an image arrives labelled as
#: something the browser will not draw and the page shows nothing — on every
#: browser at once, since it is the server that is wrong.
#:
#: These are the types Compass itself accepts, so the table is short by
#: construction. `mimetypes` remains the fallback for anything not listed,
#: which is the right way round: a wrong guess about an unexpected extension
#: costs a download prompt, while a wrong guess about a PNG costs the icon.
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def media_type_for(path: Path) -> str:
    """What to serve `path` as. Never guesses when it does not have to."""
    known = MEDIA_TYPES.get(path.suffix.lower())
    if known:
        return known
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
