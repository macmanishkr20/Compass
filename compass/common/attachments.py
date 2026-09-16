"""Attachment ingestion — shared by Home/Chat and the Agent Console.

Turns raw uploaded files (base64 data URLs from the browser) into model-ready
content:

  * raster images  -> gpt-5 vision parts (passed through as image_url),
  * PDF / DOCX      -> extracted text, inlined,
  * ZIP archives    -> each text/code entry extracted and inlined,
  * text / code     -> decoded and inlined,
  * anything else   -> a short "[binary file]" placeholder.

Extraction is best-effort and never raises to the caller: a file that can't be
read is noted inline so the turn still runs. `build_user_message()` is the one
entry point both engines call, so images and files behave identically in Chat
and in the Agent Console.
"""

from __future__ import annotations

import base64
import io
import zipfile
from typing import Any

from compass.common.models.messages import Message, user_message

# Guards so a huge upload can't blow the model's context window.
MAX_TEXT_CHARS = 200_000  # per extracted file
MAX_ZIP_ENTRIES = 300
MAX_ZIP_TOTAL_CHARS = 400_000

# PDF pages are rendered as well as read, so charts and scans are visible.
# Capped because each page is an image and images are not cheap: a hundred-page
# report rendered whole would cost more than the answer is worth, and the text
# of every page still goes up regardless of how many were drawn.
PDF_MAX_PAGES = 20
#: Render scale relative to 72dpi. 2.0 is ~144dpi.
PDF_RENDER_SCALE = 2.0
#: Longest edge, in pixels. Past this the model downscales anyway, so sending
#: more is paying to transmit detail that is discarded on arrival.
PDF_MAX_EDGE = 1568

# Extensions treated as text when pulled out of a ZIP (skip binaries/images).
_TEXT_EXTS = {
    "txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "env", "xml", "html", "htm", "css", "scss",
    "js", "jsx", "ts", "tsx", "mjs", "cjs", "vue", "svelte", "py", "pyi",
    "java", "kt", "kts", "c", "h", "cpp", "cc", "hpp", "cs", "go", "rs", "rb",
    "php", "swift", "m", "mm", "sh", "bash", "zsh", "sql", "graphql", "gql",
    "proto", "dockerfile", "gitignore", "makefile", "gradle", "properties",
    "svg", "tex", "r", "jl", "lua", "pl", "dart", "scala", "clj", "ex", "exs",
}


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _decode_data_url(data_url: str) -> bytes:
    """Bytes from a `data:...;base64,XXXX` URL (or a bare base64 string)."""
    if not data_url:
        return b""
    payload = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
    try:
        return base64.b64decode(payload)
    except Exception:  # noqa: BLE001
        return b""


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more characters]"


def _as_text(data: bytes) -> str:
    """Decode bytes as text, or flag them as binary."""
    if b"\x00" in data[:8192]:
        return f"[binary file, {len(data)} bytes — not shown as text]"
    return _clip(data.decode("utf-8", errors="replace"))


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        return _clip(text) if text.strip() else "[PDF had no extractable text]"
    except Exception as err:  # noqa: BLE001
        return f"[could not read PDF: {err}]"


def _render_pdf_pages(data: bytes) -> list[str]:
    """Each page as a PNG data URL, so the model can look at the document.

    Text extraction alone cannot see a chart, a diagram, a signature or a
    scan — and a scanned PDF extracts to nothing at all, which is how a
    document full of content arrives as "[PDF had no extractable text]".
    Anthropic's PDF support renders every page to an image and sends it
    alongside the extracted text for exactly this reason.

    Azure does not do it for us. Confirmed rather than assumed: a one-page PDF
    whose only distinguishing content was a red line above a blue line went up
    as `input_file`, was accepted, and the model answered "CANNOT SEE" when
    asked the colours. Text is all it gets, which is all Compass was giving it
    anyway.

    Returns an empty list, quietly, when no rasteriser is installed. The text
    path is unchanged and still runs, so a deployment without the optional
    dependency behaves exactly as it did before rather than failing.
    """
    try:
        import pypdfium2
    except ImportError:
        return []
    try:
        import base64 as _b64

        pdf = pypdfium2.PdfDocument(io.BytesIO(data))
        out: list[str] = []
        pages_to_draw = min(len(pdf), PDF_MAX_PAGES)
        for index in range(pages_to_draw):
            page = pdf[index]
            # `scale` is relative to 72dpi. 2.0 is ~144dpi, which keeps small
            # print legible without producing an image the model will only
            # downscale again.
            image = page.render(scale=PDF_RENDER_SCALE).to_pil()
            if max(image.size) > PDF_MAX_EDGE:
                ratio = PDF_MAX_EDGE / max(image.size)
                image = image.resize(
                    (max(int(image.width * ratio), 1),
                     max(int(image.height * ratio), 1))
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            out.append("data:image/png;base64,"
                       + _b64.b64encode(buffer.getvalue()).decode())
            page.close()
        # Closed rather than left to the garbage collector: pypdfium2 holds a
        # native handle and complains loudly at interpreter shutdown when one
        # is still open.
        pdf.close()
        return out
    except Exception:  # noqa: BLE001 — the text already went; this is a bonus
        return []


def _extract_docx(data: bytes) -> str:
    try:
        import docx

        doc = docx.Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
        return _clip(text) if text.strip() else "[DOCX had no extractable text]"
    except Exception as err:  # noqa: BLE001
        return f"[could not read DOCX: {err}]"


def _extract_zip(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as err:  # noqa: BLE001
        return f"[could not read ZIP: {err}]"
    out: list[str] = []
    total = 0
    for info in zf.infolist():
        if info.is_dir() or len(out) >= MAX_ZIP_ENTRIES:
            continue
        name = info.filename
        if _ext(name) not in _TEXT_EXTS and "." in name:
            continue  # skip binaries/images inside the archive
        try:
            raw = zf.read(info)
        except Exception:  # noqa: BLE001
            continue
        body = _as_text(raw)
        chunk = f"--- {name} ---\n{body}"
        total += len(chunk)
        out.append(chunk)
        if total >= MAX_ZIP_TOTAL_CHARS:
            out.append("… [archive truncated]")
            break
    if not out:
        return "[ZIP contained no readable text files]"
    return "\n\n".join(out)


def extract_document_text(name: str, mime: str, data_url: str) -> str:
    """The text of one uploaded document, for a caller that reads rather than
    looks: PDF and DOCX extracted, anything else decoded as text.

    No page rendering — that is for a model being shown the pages, and a
    forty-page requirements document rendered to images is slow work nobody
    reads. Never raises: a file that cannot be read comes back as the same
    bracketed note the other paths use, so the caller can say why.
    """
    ext = _ext(name)
    mime = (mime or "").lower()
    try:
        data = _decode_data_url(data_url)
    except Exception as err:  # noqa: BLE001
        return f"[could not read the upload: {err}]"
    if ext == "pdf" or mime == "application/pdf":
        return _extract_pdf(data)
    if ext == "docx" or "wordprocessingml" in mime:
        return _extract_docx(data)
    return _clip(_as_text(data))


def process_attachment(att: dict) -> dict | None:
    """Normalize one raw upload `{name, mime, data_url}` into either
    `{kind:'image', name, data_url}` or `{kind:'text', name, text}`."""
    name = (att.get("name") or "file").strip()
    mime = (att.get("mime") or "").lower()
    data_url = att.get("data_url") or ""
    # Some callers may pre-extract text (kept for compatibility).
    if att.get("text") is not None and not data_url:
        return {"kind": "text", "name": name, "text": _clip(str(att["text"]))}

    ext = _ext(name)
    if mime.startswith("image/") and mime != "image/svg+xml":
        return {"kind": "image", "name": name, "data_url": data_url}

    data = _decode_data_url(data_url)
    if ext == "pdf" or mime == "application/pdf":
        # Both: the text for quoting and searching, the pages for looking at.
        return {"kind": "text", "name": name, "text": _extract_pdf(data),
                "page_images": _render_pdf_pages(data)}
    if ext == "docx" or "wordprocessingml" in mime:
        return {"kind": "text", "name": name, "text": _extract_docx(data)}
    if ext == "zip" or mime in ("application/zip", "application/x-zip-compressed"):
        return {"kind": "text", "name": name, "text": _extract_zip(data)}
    # svg + any text/code file
    return {"kind": "text", "name": name, "text": _as_text(data)}


def build_user_message(text: str, raw_attachments: list[dict] | None) -> Message:
    """The one message builder for both engines. Text attachments are inlined
    as fenced blocks; images become multimodal image_url parts for gpt-5
    vision. With no images the content stays a plain string (cheapest path)."""
    processed = [
        p for p in (process_attachment(a) for a in (raw_attachments or [])) if p
    ]
    text_files = [p for p in processed if p["kind"] == "text"]
    images = [p["data_url"] for p in processed
              if p["kind"] == "image" and p.get("data_url")]
    # Rendered PDF pages ride along with the uploaded images. They are the
    # same thing to the model: pictures of something it was asked to read.
    pages = [(p["name"], p.get("page_images") or []) for p in text_files]

    body = text or ""
    for f in text_files:
        body += f"\n\n--- Attached file: {f['name']} ---\n```\n{f.get('text', '')}\n```"
    for name, rendered in pages:
        if rendered:
            body += (f"\n\n[{len(rendered)} page(s) of {name} are attached as "
                     "images below, in order, so you can see anything the text "
                     "extraction could not — charts, diagrams, layout, scans.]")

    rendered_all = [url for _, urls in pages for url in urls]
    if not images and not rendered_all:
        return user_message(body)

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": body or "(see the attached image)"}
    ]
    for url in [*images, *rendered_all]:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return Message(role="user", content=parts)  # type: ignore[arg-type]
