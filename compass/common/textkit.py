"""Setting type for video — including scripts Pillow cannot set on its own.

Pillow draws a string by walking its code points and stamping one glyph per
code point. That is correct for Latin and wrong for most of India. In Kannada,
`ಒಟ್ಟಾಗಿ` is eight code points and five glyphs: consonants fuse into conjuncts,
and vowel signs reorder around the letter they belong to. Stamped one by one
they come out as separate pieces with dotted circles where a mark failed to
attach — which is what this machine's Pillow does, because its Raqm support
(HarfBuzz + FriBidi) is not compiled in, and no wheel is going to change that.

So shaping is done here: HarfBuzz works out which glyphs and where, FreeType
rasterises them, and they are composited into a Pillow image. Both arrive as
ordinary wheels with no system libraries behind them, so this works the same
on the Mac it was written on and the Windows machine it has to run on.

Without them the module still works — it falls back to Pillow's own layout,
which is fine for Latin and visibly wrong for Indic. `shaping_available()`
says which of the two you are getting, so a caller can warn rather than
quietly produce something unreadable.

Font choice is by script rather than by name. A teaser captioned in Kannada
and translated into English needs two faces in the same frame, and asking the
caller to name them is asking them to know what is installed.
"""

from __future__ import annotations

import functools
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("compass.textkit")

try:  # both are optional; Latin still sets without them
    import uharfbuzz as _hb
    import freetype as _ft
    _SHAPING = True
except ImportError:  # pragma: no cover - depends on the install
    _hb = _ft = None
    _SHAPING = False


def shaping_available() -> bool:
    """True when Indic, Arabic and other complex scripts will come out right."""
    return _SHAPING


#: Unicode blocks worth telling apart, because each wants a different face.
#: Ranges are the primary block for the script; the lookup takes the first
#: character that lands in one, so a Kannada line with an English word in it
#: is still set in the Kannada face.
_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0900, 0x097F, "devanagari"),
    (0x0980, 0x09FF, "bengali"),
    (0x0A00, 0x0A7F, "gurmukhi"),
    (0x0A80, 0x0AFF, "gujarati"),
    (0x0B00, 0x0B7F, "oriya"),
    (0x0B80, 0x0BFF, "tamil"),
    (0x0C00, 0x0C7F, "telugu"),
    (0x0C80, 0x0CFF, "kannada"),
    (0x0D00, 0x0D7F, "malayalam"),
    (0x0E00, 0x0E7F, "thai"),
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x4E00, 0x9FFF, "han"),
    (0x3040, 0x30FF, "kana"),
    (0xAC00, 0xD7AF, "hangul"),
)

#: Where each script's faces live, most-wanted first, across the three
#: platforms this runs on. Missing files are skipped, so a list may name
#: fonts that exist on one machine and not another.
_FACES: dict[str, tuple[str, ...]] = {
    "kannada": (
        "/System/Library/Fonts/Supplemental/Kannada Sangam MN.ttc",
        "/System/Library/Fonts/Supplemental/Kannada MN.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf",
        "C:\\Windows\\Fonts\\Nirmala.ttf",
        "C:\\Windows\\Fonts\\tunga.ttf",
    ),
    "devanagari": (
        "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
        "/System/Library/Fonts/Supplemental/DevanagariMT.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "C:\\Windows\\Fonts\\Nirmala.ttf",
        "C:\\Windows\\Fonts\\mangal.ttf",
    ),
    "tamil": ("/System/Library/Fonts/Supplemental/Tamil Sangam MN.ttc",
              "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
              "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "telugu": ("/System/Library/Fonts/Supplemental/Telugu Sangam MN.ttc",
               "/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf",
               "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "malayalam": ("/System/Library/Fonts/Supplemental/Malayalam Sangam MN.ttc",
                  "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf",
                  "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "bengali": ("/System/Library/Fonts/Supplemental/Bangla Sangam MN.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
                "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "gujarati": ("/System/Library/Fonts/Supplemental/Gujarati Sangam MN.ttc",
                 "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "gurmukhi": ("/System/Library/Fonts/Supplemental/Gurmukhi Sangam MN.ttc",
                 "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "oriya": ("/System/Library/Fonts/Supplemental/Oriya Sangam MN.ttc",
              "C:\\Windows\\Fonts\\Nirmala.ttf"),
    "thai": ("/System/Library/Fonts/Supplemental/Thonburi.ttc",
             "C:\\Windows\\Fonts\\tahoma.ttf"),
    "arabic": ("/System/Library/Fonts/Supplemental/Geeza Pro.ttc",
               "C:\\Windows\\Fonts\\arial.ttf"),
    "hebrew": ("/System/Library/Fonts/Supplemental/Arial Hebrew.ttc",
               "C:\\Windows\\Fonts\\arial.ttf"),
    "han": ("/System/Library/Fonts/Hiragino Sans GB.ttc",
            "C:\\Windows\\Fonts\\msyh.ttc"),
    "kana": ("/System/Library/Fonts/Hiragino Sans GB.ttc",
             "C:\\Windows\\Fonts\\msgothic.ttc"),
    "hangul": ("/System/Library/Fonts/AppleSDGothicNeo.ttc",
               "C:\\Windows\\Fonts\\malgun.ttf"),
}

#: Latin, which is also the last resort for a script with nothing installed:
#: tofu in a known face beats a crash.
_LATIN_DISPLAY = (
    "/System/Library/Fonts/Supplemental/Didot.ttc",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/Library/Fonts/Georgia.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "C:\\Windows\\Fonts\\georgia.ttf",
    "C:\\Windows\\Fonts\\times.ttf",
)
_LATIN_BOLD = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\segoeuib.ttf",
)
_LATIN_REGULAR = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
)


def script_of(text: str) -> str:
    """The script this line is written in — the first character that belongs
    to one wins, so a stray ASCII digit does not make a Kannada line Latin."""
    for ch in text:
        code = ord(ch)
        if code < 0x0370:  # ASCII and Latin-1 supplements: keep looking
            continue
        for start, end, name in _SCRIPT_RANGES:
            if start <= code <= end:
                return name
    return "latin"


@functools.lru_cache(maxsize=64)
def _first_existing(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        if Path(path).is_file():
            return path
    return None


@functools.lru_cache(maxsize=128)
def font_for(text: str, *, weight: str = "regular") -> str | None:
    """A font file able to set `text`.

    `weight` is display | bold | regular, and applies to Latin only: the Indic
    faces installed on a normal machine come in one weight, and asking for a
    bold that is not there would fall through to a face that cannot draw the
    script at all — which is a worse outcome than regular weight.
    """
    script = script_of(text)
    if script != "latin":
        if found := _first_existing(_FACES.get(script, ())):
            return found
        logger.warning("textkit: no font installed for %s; it will not render", script)
    if weight == "display":
        return _first_existing(_LATIN_DISPLAY) or _first_existing(_LATIN_REGULAR)
    if weight == "bold":
        return _first_existing(_LATIN_BOLD)
    return _first_existing(_LATIN_REGULAR)


@dataclass
class Run:
    """A shaped line, ready to be stamped at a position."""

    glyphs: list[tuple[int, float, float]]  # glyph id, x offset, y offset
    width: float
    font_path: str
    size: int


def _shape(text: str, font_path: str, size: int, tracking: float) -> Run:
    """HarfBuzz decides the glyphs and their positions; nothing else can."""
    blob = _hb.Blob.from_file_path(font_path)
    face = _hb.Face(blob)
    font = _hb.Font(face)
    font.scale = (size * 64, size * 64)
    _hb.ot_font_set_funcs(font)
    buf = _hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()  # script, direction and language from the text
    _hb.shape(font, buf)

    glyphs: list[tuple[int, float, float]] = []
    pen = 0.0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        glyphs.append((info.codepoint, pen + pos.x_offset / 64, pos.y_offset / 64))
        pen += pos.x_advance / 64 + tracking
    return Run(glyphs=glyphs, width=max(0.0, pen - tracking), font_path=font_path,
               size=size)


@functools.lru_cache(maxsize=32)
def _ft_face(font_path: str, size: int):
    face = _ft.Face(font_path)
    face.set_char_size(size * 64)
    return face


def _pillow_font(font_path: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(font_path, size)
    except Exception:  # noqa: BLE001 - a .ttc index Pillow dislikes, say
        return ImageFont.load_default(size)


def measure(text: str, font_path: str, size: int, *, tracking: float = 0.0) -> float:
    """How wide this line will be, in pixels."""
    if not text:
        return 0.0
    if _SHAPING:
        return _shape(text, font_path, size, tracking).width
    from PIL import ImageDraw, Image

    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    return draw.textlength(text, font=_pillow_font(font_path, size)) + tracking * len(text)


def draw(image, xy: tuple[float, float], text: str, font_path: str, size: int,
         fill: tuple[int, int, int, int], *, tracking: float = 0.0) -> float:
    """Stamp `text` with its baseline-left at `xy`. Returns the advance width.

    RGBA in, RGBA out — these are layers to be composited over video, so the
    colour is painted through the glyph's coverage mask rather than onto an
    opaque background.
    """
    if not text:
        return 0.0
    if not _SHAPING:
        from PIL import ImageDraw

        draw_ = ImageDraw.Draw(image)
        font = _pillow_font(font_path, size)
        # Pillow's anchor "ls" is baseline-left, matching the shaped path.
        draw_.text(xy, text, font=font, fill=fill, anchor="ls")
        return measure(text, font_path, size, tracking=tracking)

    from PIL import Image

    run = _shape(text, font_path, size, tracking)
    face = _ft_face(font_path, size)
    x0, baseline = xy
    for glyph_id, dx, dy in run.glyphs:
        face.load_glyph(glyph_id, _ft.FT_LOAD_RENDER | _ft.FT_LOAD_TARGET_NORMAL)
        bitmap = face.glyph.bitmap
        if not bitmap.width or not bitmap.rows:
            continue  # a space, or a mark with no ink
        mask = Image.frombytes("L", (bitmap.width, bitmap.rows), bytes(bitmap.buffer))
        px = int(round(x0 + dx + face.glyph.bitmap_left))
        py = int(round(baseline - dy - face.glyph.bitmap_top))
        # Paste the colour through the glyph as a mask, so overlapping marks
        # in a conjunct blend instead of punching holes in each other.
        image.paste(Image.new("RGBA", mask.size, fill), (px, py), mask)
    return run.width


def wrap(text: str, font_path: str, size: int, max_width: float, *,
         tracking: float = 0.0) -> list[str]:
    """Break `text` into lines that fit, measured rather than counted.

    Scripts that do not put spaces between words (Han, Kana, Thai) break
    anywhere, because a greedy word wrap on them returns one very long line.
    """
    text = text.strip()
    if not text:
        return []
    if script_of(text) in {"han", "kana", "thai"}:
        lines, line = [], ""
        for ch in text:
            if measure(line + ch, font_path, size, tracking=tracking) > max_width and line:
                lines.append(line)
                line = ch
            else:
                line += ch
        return lines + ([line] if line else [])

    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if measure(trial, font_path, size, tracking=tracking) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def line_height(font_path: str, size: int, *, factor: float = 1.3) -> int:
    """Leading for this face at this size.

    Asked of the face rather than assumed, because Indic faces carry tall
    ascenders and descenders for the marks that sit above and below the line,
    and Latin leading applied to them overlaps the marks of the line beneath.
    """
    if _SHAPING:
        try:
            face = _ft_face(font_path, size)
            metrics = face.size
            natural = (metrics.ascender - metrics.descender) / 64
            return int(max(size * factor, natural * (factor / 1.3)))
        except Exception:  # noqa: BLE001
            pass
    return int(size * factor)


def uppercase_spaced(text: str) -> str:
    """The small line under a title, in the manner of a title card.

    Only for scripts that have case. Applying it to Kannada would do nothing
    to the letters and add gaps inside conjuncts, which is worse than leaving
    it alone.
    """
    if script_of(text) != "latin":
        return text
    return unicodedata.normalize("NFC", text).upper()
