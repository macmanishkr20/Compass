"""Where a chat thread's uploads live, so something can be made from them.

Home reads attachments and forgets them: a photo becomes a vision part, a
recording becomes a transcript, and the bytes are gone by the end of the turn.
That is right for looking and reading, and useless for rendering — you cannot
cut a teaser out of a base64 string the model has already been shown.

So media attachments are also written down, under `sessions_dir/chat_media/
<session>/`, and handed to the render tool as paths. Three properties this
keeps, deliberately:

  * one directory per thread, named by session id, so an upload is reachable
    from the conversation it arrived in and from nowhere else;
  * names are rewritten on the way in — `0003-holiday.jpg`, never whatever the
    browser said — so a filename cannot walk out of the directory;
  * `resolve()` re-checks containment after resolving symlinks, because the
    id it is given comes from a model that is reading the user's captions.

Nothing here deletes anything on a schedule. Uploads belong to the thread and
go when the thread does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from compass.common.attachments import media_kind
from compass.common.config import get_settings
from compass.common.persistence import blob

logger = logging.getLogger("compass.home.media")

#: A whole conversation's worth of uploads. Generous for a teaser, bounded so
#: a thread cannot quietly become the largest thing on the disk.
MAX_SESSION_BYTES = 600 * 1024 * 1024

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: The file that remembers what each upload was called before it was
#: sanitised. Without it the only record of the name is the sanitised one, and
#: a name written in Kannada sanitises to nothing at all — so the model is
#: shown a file called `0005-mp3` for something the person called
#: `ಗಣೇಶ ಹಾಡು.mp3`, and asking for it by the name they used finds nothing.
_MANIFEST = "_uploads.json"

#: A stem to fall back on per kind, for a name that sanitises away entirely.
_FALLBACK_STEM = {"image": "photo", "video": "clip", "sound": "audio"}


@dataclass
class MediaFile:
    """One kept upload, as the model is told about it."""

    id: str          # "0003-holiday.jpg" — the filename, which is also the id
    name: str        # what it was called when it was uploaded
    kind: str        # image | video | sound
    path: Path
    bytes: int
    #: Pixels, for pictures — so an editor knows which way up it is without
    #: opening it.
    width: int = 0
    height: int = 0
    #: One line saying what is in it, and a word for where it belongs in a
    #: film. Written once by compass.home.vision; empty until then.
    note: str = ""
    beat: str = ""

    @property
    def shape(self) -> str:
        if not self.width or not self.height:
            return ""
        if self.width == self.height:
            return "square"
        return "portrait" if self.height > self.width else "landscape"

    def describe(self) -> str:
        bits = [f"{self.id} — {self.kind}"]
        if self.shape:
            bits.append(f"{self.shape} {self.width}x{self.height}")
        if self.beat:
            bits.append(self.beat)
        line = ", ".join(bits)
        return f"{line}: {self.note}" if self.note else f"{line}, as {self.name}"


#: A session id is a UUID: hex and dashes, nothing else. Narrower than
#: `_SAFE`, which allows a dot because filenames need one — and which
#: therefore let `..` through untouched. `session_dir("..")` used to resolve
#: to the sessions directory itself, one level above the media root, so
#: anything built on it was operating on the transcripts. Nothing exploited
#: that while every caller passed a server-minted id and only ever read or
#: wrote inside the result; it became a way to delete the transcripts the
#: moment a caller wanted to remove a directory.
_SAFE_ID = re.compile(r"[^A-Za-z0-9-]+")


def session_dir(session_id: str, *, create: bool = False) -> Path:
    """This thread's media directory. `session_id` is a UUID minted by the
    server, but it arrives here through a URL, so it is sanitised anyway.

    The result is always a directory *inside* the media root — checked, not
    assumed, because that is the property every caller relies on.
    """
    safe = _SAFE_ID.sub("", session_id)[:64] or "unknown"
    root = get_settings().sessions_dir / "chat_media"
    path = root / safe
    if root.resolve() not in path.resolve().parents:
        raise ValueError(f"unusable session id: {session_id!r}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


#: The first bytes of a file say what it is, whatever it is called. Used for
#: uploads kept before names carried their extension reliably — a song saved
#: as `0007-Y2Mate.is---Ya-Re-Ya-_-Lyrical-` is still an MP3, and reading
#: twelve bytes is a cheaper way to know that than asking the person to
#: upload it again.
def sniff_kind(path: Path) -> str:
    """image | video | sound | other, from the file's own first bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(12)
    except OSError:
        return "other"
    if len(head) < 4:
        return "other"
    if head[:3] == b"ID3" or (head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "sound"  # MP3, tagged or bare
    if head[:4] in (b"OggS", b"fLaC"):
        return "sound"
    if head[:4] == b"RIFF":
        return {b"WAVE": "sound", b"WEBP": "image", b"AVI ": "video"}.get(
            head[8:12], "other")
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"M4A"):
            return "sound"
        if brand[:3] in (b"hei", b"mif", b"avi"):
            return "image"
        return "video"
    if head[:3] == b"\xff\xd8\xff" or head[:4] == b"\x89PNG" or head[:4] == b"GIF8":
        return "image"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    return "other"


def _manifest(directory: Path) -> dict[str, dict]:
    path = directory / _MANIFEST
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(directory: Path, rows: dict[str, dict]) -> None:
    try:
        (directory / _MANIFEST).write_text(json.dumps(rows))
    except OSError as err:  # noqa: BLE001 — the file itself is already saved
        logger.warning("chat_media: could not write the name index: %s", err)


def _safe_filename(name: str, kind: str, index: int) -> str:
    """A name safe to put on disk that still ends in the right extension.

    The extension is carried across separately rather than being left to
    survive the sanitiser: `ಗಣೇಶ ಹಾಡು.mp3` has every character before the dot
    stripped, and `.strip("-.")` then eats the dot too, leaving `mp3` — a file
    with no extension, which every later step reads as "not media at all".
    """
    stem, _, ext = name.rpartition(".")
    if not stem:  # no dot in the name
        stem, ext = name, ""
    safe_stem = _SAFE.sub("-", stem).strip("-.")[:60]
    safe_ext = _SAFE.sub("", ext).lower()[:8]
    if not safe_stem:
        safe_stem = _FALLBACK_STEM.get(kind, "file")
    return f"{index:04d}-{safe_stem}" + (f".{safe_ext}" if safe_ext else "")


def _decode(data_url: str) -> bytes:
    if not data_url:
        return b""
    payload = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
    try:
        return base64.b64decode(payload)
    except Exception:  # noqa: BLE001 — a bad upload is not a crash
        return b""


def keep(session_id: str, attachments: list[dict] | None) -> list[MediaFile]:
    """Write the photos, clips and recordings in `attachments` to this
    thread's directory, and say what was kept.

    Never raises. A file that cannot be written is logged and left out; the
    turn still runs, and the model simply is not told about a file it cannot
    use anyway.
    """
    if not attachments:
        return []
    directory = session_dir(session_id, create=True)
    existing = [p for p in (directory.iterdir() if directory.exists() else [])
                if p.name != _MANIFEST]
    used = sum(p.stat().st_size for p in existing if p.is_file())
    index = len(existing)
    rows = _manifest(directory)
    kept: list[MediaFile] = []

    for att in attachments:
        name = (att.get("name") or "file").strip()
        kind = media_kind(name, att.get("mime") or "")
        if kind == "other":
            continue
        data = _decode(att.get("data_url") or "")
        if not data:
            continue
        if used + len(data) > MAX_SESSION_BYTES:
            logger.warning("chat_media: %s skipped, session at its size limit", name)
            continue
        index += 1
        target = directory / _safe_filename(name, kind, index)
        try:
            target.write_bytes(data)
        except OSError as err:
            logger.warning("chat_media: could not keep %s: %s", name, err)
            continue
        used += len(data)
        row = {"name": name, "kind": kind, "mime": att.get("mime") or ""}
        if kind == "image":
            # Read once here rather than on every listing: knowing a photo is
            # upright is what stops it being laid out as though it were wide.
            try:
                from PIL import Image

                with Image.open(target) as probe:
                    row["width"], row["height"] = probe.size
            except Exception:  # noqa: BLE001 — dimensions are a nicety
                pass
        rows[target.name] = row
        kept.append(MediaFile(id=target.name, name=name, kind=kind,
                              path=target, bytes=len(data)))
    if kept:
        _write_manifest(directory, rows)
    return kept


def listing(session_id: str) -> list[MediaFile]:
    """Everything kept for this thread, oldest first — the numeric prefix is
    the upload order, so sorting by name sorts by time."""
    directory = session_dir(session_id)
    if not directory.is_dir():
        return []
    rows = _manifest(directory)
    out: list[MediaFile] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.name == _MANIFEST:
            continue
        row = rows.get(path.name, {})
        # The manifest knows both; the filename is the fallback for anything
        # kept before it existed; and the bytes are the last word, for a file
        # whose name lost its extension on the way in.
        kind = row.get("kind") or media_kind(path.name)
        if kind == "other":
            kind = sniff_kind(path)
        if kind == "other":
            continue
        name = row.get("name") or path.name.split("-", 1)[-1]
        out.append(MediaFile(id=path.name, name=name, kind=kind, path=path,
                             bytes=path.stat().st_size,
                             width=int(row.get("width") or 0),
                             height=int(row.get("height") or 0),
                             note=str(row.get("note") or ""),
                             beat=str(row.get("beat") or "")))
    return out


def annotate(session_id: str, notes: dict[str, dict]) -> None:
    """Record what the pictures are of, keyed by id. Merged into whatever the
    manifest already holds, so a second pass adds rather than replaces."""
    directory = session_dir(session_id)
    if not directory.is_dir():
        return
    rows = _manifest(directory)
    for file_id, fields in notes.items():
        row = rows.setdefault(file_id, {})
        row.update({k: v for k, v in fields.items() if v})
    _write_manifest(directory, rows)


def resolve(session_id: str, ref: str) -> Path | None:
    """The file `ref` names inside this thread, or None.

    `ref` is whatever the model wrote, so containment is checked after
    resolution rather than by inspecting the string: `..` is the obvious
    attack and a symlink is the one that survives a string check.
    """
    if not ref:
        return None
    directory = session_dir(session_id)
    if not directory.is_dir():
        return None
    candidate = (directory / Path(ref).name).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    # A forgiving second pass: the model may have written the original
    # filename rather than the id it was given — and the two differ whenever
    # the name had to be sanitised, which is most names with a space or a
    # bracket in them. Compared with punctuation removed so that
    # "My Song (Official).mp3" matches "0002-My-Song-Official-.mp3".
    wanted = _loose(Path(ref).name)
    if not wanted:
        return None
    files = listing(session_id)
    for file in files:
        if _loose(file.name) == wanted or _loose(file.id) == wanted:
            return file.path
        # Also match the id with its numeric prefix dropped, for a model
        # that quoted the stored name without the ordering number.
        if _loose(file.id.split("-", 1)[-1]) == wanted:
            return file.path

    # Last resort, for uploads kept before names carried their extension: the
    # stored name is a truncation of the real one, so neither is equal to the
    # other but one starts with the other. Long prefixes only, and only when
    # exactly one file matches — a guess between two candidates would return
    # the wrong song, which is worse than saying it cannot be found.
    hits = [f for f in files
            if _prefix_match(wanted, _loose(f.id.split("-", 1)[-1]))
            or _prefix_match(wanted, _loose(f.name))]
    return hits[0].path if len(hits) == 1 else None


#: Short prefixes match too many things; this is about the length of a couple
#: of words.
_MIN_PREFIX = 16


def _prefix_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= _MIN_PREFIX and longer.startswith(shorter)


def _loose(name: str) -> str:
    """A name reduced to what two spellings of it have in common: letters and
    digits, lowercased, punctuation and spacing dropped.

    Unicode-aware deliberately. `[^a-z0-9]` would reduce every Kannada and
    Hindi filename to its extension alone, so `ಗಣೇಶ ಹಾಡು.mp3` and `गीत.mp3`
    would both become "mp3" and resolve to whichever was uploaded first —
    a worse failure than the one this is here to fix, because it returns the
    wrong file rather than none.
    """
    return re.sub(r"[\W_]+", "", (name or "").lower(), flags=re.UNICODE)


def output_path(session_id: str, filename: str) -> Path:
    """Where a render writes. Same directory, same naming rules — a rendered
    teaser is one more file belonging to the thread, and it is served by the
    same route that serves the uploads."""
    directory = session_dir(session_id, create=True)
    safe = _SAFE.sub("-", filename).strip("-.") or "teaser.mp4"
    if not safe.lower().endswith(".mp4"):
        safe += ".mp4"
    target = directory / safe
    # Never overwrite an earlier render: someone who asked for two versions
    # wants two, and the first one is already linked in the transcript above.
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = directory / f"{stem}-{n}{suffix}"
        n += 1
    return target


def url_for(session_id: str, filename: str) -> str:
    """The address the browser fetches this file from. Served by
    `GET /v1/chat/sessions/{id}/media/{name}` in home/routes.py."""
    return f"/v1/chat/sessions/{session_id}/media/{filename}"


# --------------------------------------------------------------------------- #
# Blob storage: where these actually live.
#
# ffmpeg needs files, and a path is what the render tool is given, so the
# thread's directory stays exactly as it was and keeps working the same way.
# What changes is which copy is the lasting one: the directory is now a working
# copy of the container, filled in when it is missing and written through when
# it changes.
#
# That matters because the directory was the only copy — 286MB of photographs,
# clips and recordings here, on one laptop, gitignored and unbacked. It is also
# the last thing in Compass that was not where the rest of the data is.
#
# Under its own `chat/` prefix in `compass-media`, beside the `messages/`
# prefix the oversized message content uses, so neither can reach the other's
# files when one of them is being cleared.
# --------------------------------------------------------------------------- #

BLOB_PREFIX = "chat"


def _blob_enabled() -> bool:
    return blob.enabled()


def _blob_name(session_id: str, filename: str) -> str:
    return f"{BLOB_PREFIX}/{session_id}/{filename}"


def _container():
    return blob.container("compass-media")


def _upload_sync(name: str, path: Path) -> None:
    with path.open("rb") as handle:
        _container().upload_blob(
            name, handle, overwrite=True, max_concurrency=blob.CONCURRENCY
        )


def _download_sync(name: str, path: Path) -> None:
    # Into a temporary name and then moved, so a download interrupted halfway
    # cannot leave a half-file that `listing` would report as a playable clip.
    tmp = path.with_name(path.name + ".part")
    with tmp.open("wb") as handle:
        _container().download_blob(name, max_concurrency=blob.CONCURRENCY).readinto(handle)
    tmp.replace(path)


def _names_sync(session_id: str) -> dict[str, int]:
    prefix = f"{_blob_name(session_id, '')}"
    return {
        b.name.split("/", 2)[-1]: b.size
        for b in _container().list_blobs(name_starts_with=prefix)
    }


async def upload(
    session_id: str,
    files: list[MediaFile] | None = None,
    *,
    owner: str = "",
) -> int:
    """Write this thread's files through to blob storage. Returns how many.

    `files` names what just arrived; without it, everything in the directory
    that is not already stored. The manifest goes too — it is the only record
    of what each upload was really called.

    Each file that lands also gets a row in the media index, under `owner` and
    this thread. The bytes were already reachable without one — the serving
    route builds its path from the session id — but only by somebody who
    already knew which conversation to look in. The row is what makes an
    upload findable as a thing this person has, from a browser that has never
    seen this machine, which is the whole point of putting it in blob storage
    rather than leaving it on disk.

    Never raises. A file that could not be uploaded is still on disk and still
    works; the turn that was saving it is not the place to fail.
    """
    if not _blob_enabled():
        return 0
    directory = session_dir(session_id)
    if not directory.is_dir():
        return 0
    #: What the caller already knows each file to be. A rendered film is
    #: handed over as one; sniffing would agree, but only by reading the
    #: bytes back off the disk it was just written to.
    declared = {f.path.name: f.kind for f in (files or [])}
    if files is None:
        wanted = [p for p in sorted(directory.iterdir()) if p.is_file()]
    else:
        wanted = [f.path for f in files]
        manifest = directory / _MANIFEST
        if manifest.is_file():
            wanted.append(manifest)

    async def one(path: Path) -> bool:
        name = _blob_name(session_id, path.name)
        try:
            await asyncio.to_thread(_upload_sync, name, path)
        except Exception as err:  # noqa: BLE001 — on disk already; log and move on
            logger.warning("could not upload %s: %s", path.name, err)
            return False
        # The manifest is bookkeeping about the uploads, not one of them.
        if path.name != _MANIFEST:
            from compass.common import media_index

            kind = declared.get(path.name) or sniff_kind(path)
            await media_index.record(
                blob_name=name,
                url=url_for(session_id, path.name),
                # A film is a film wherever it came from; everything else a
                # thread holds is "upload", which is what it is — the index
                # keeps the distinction the person would make, not the codec.
                kind="video" if kind == "video" else "upload",
                owner=owner,
                session_id=session_id,
                size_bytes=path.stat().st_size if path.is_file() else 0,
            )
        return True

    done = await asyncio.gather(*(one(p) for p in wanted))
    return sum(done)


async def hydrate(session_id: str) -> int:
    """Make sure this thread's files are on disk. Returns how many came down.

    Does nothing at all in the normal case, where they are already there — one
    listing of the prefix and no transfers. It earns its place on a machine
    that has the conversation but not the files: a second laptop, a rebuilt
    container, a disk that was cleared.

    Compared by size as well as presence, so a file that was truncated or
    half-written is fetched again rather than trusted.
    """
    if not _blob_enabled():
        return 0
    try:
        remote = await asyncio.to_thread(_names_sync, session_id)
    except Exception as err:  # noqa: BLE001 — offline is not a failed turn
        logger.warning("could not list stored media for %s: %s", session_id, err)
        return 0
    if not remote:
        return 0
    directory = session_dir(session_id, create=True)

    async def one(name: str, size: int) -> bool:
        path = directory / name
        try:
            if path.is_file() and path.stat().st_size == size:
                return False
            await asyncio.to_thread(_download_sync, _blob_name(session_id, name), path)
            return True
        except Exception as err:  # noqa: BLE001
            logger.warning("could not fetch %s: %s", name, err)
            return False

    done = await asyncio.gather(*(one(n, s) for n, s in remote.items()))
    got = sum(done)
    if got:
        logger.info("fetched %d stored file(s) for %s", got, session_id)
    return got


def _forget_blobs_sync(session_id: str) -> int:
    client = _container()
    names = [b.name for b in client.list_blobs(name_starts_with=_blob_name(session_id, ""))]
    for name in names:
        client.delete_blob(name)
    return len(names)


async def forget(session_id: str) -> int:
    """Throw away a deleted thread's uploads, on disk and in storage. Returns
    how many files went.

    This module has always said uploads go when the thread does, and they did
    not: deleting a conversation removed its transcript and left its photos
    and clips for good. 286MB of them here, belonging to threads that no
    longer exist and that nothing can reach.

    Not fatal, ever. A thread the user deleted is deleted whether or not the
    cleanup succeeded — the alternative is a conversation that cannot be
    removed because one file is busy.
    """
    gone = 0
    if _blob_enabled():
        try:
            gone += await asyncio.to_thread(_forget_blobs_sync, session_id)
        except Exception as err:  # noqa: BLE001 — best-effort
            logger.warning("could not clear stored media for %s: %s", session_id, err)
    directory = session_dir(session_id)
    if not directory.is_dir():
        return gone
    try:
        for path in sorted(directory.iterdir()):
            if path.is_file():
                path.unlink(missing_ok=True)
                gone += 1
        directory.rmdir()
    except OSError as err:
        logger.warning("could not clear media for %s: %s", session_id, err)
    return gone
