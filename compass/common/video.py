"""Teaser rendering — a storyboard of photos, clips and music becomes an MP4.

The sibling of `speech.py`: that one turns a recording into words, this one
turns what somebody uploaded into a film. Both are plain functions over files,
with no idea a chat exists.

Why a storyboard rather than a shell. Rendering video is usually done by
writing ffmpeg or MoviePy code and running it in a sandbox — which is what a
coding agent with a sandbox does, and what the Agent Console can already do
with `bash`. Home has no shell on purpose: it cannot read your files and
cannot run commands, and that is worth keeping. So the model writes the
storyboard — this photo for two seconds, that caption, the music from here —
and this module executes it. The model still directs; it just does not get an
interpreter to do it with.

The split inside: Pillow draws (type, cards, caption scrims), ffmpeg moves
(Ken Burns, crossfades, audio). Text drawn as a transparent layer and overlaid
*after* the motion, so a caption sits still while the photo drifts behind it —
burning text into the frame before `zoompan` would zoom the words too.

Each shot renders to its own intermediate file before one final assembly pass.
A single `filter_complex` spanning thirty inputs is faster and completely
opaque when it fails; per-shot files mean a failure names the shot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from compass.common import textkit

logger = logging.getLogger("compass.video")


class VideoUnavailable(RuntimeError):
    """ffmpeg is not installed. Said plainly, once, with how to fix it."""


class RenderError(RuntimeError):
    """A render that failed, named by the shot that failed it."""


#: Frame sizes per aspect. 1080-wide vertical is what a phone shows full
#: screen; the others match it in area so encoding settings carry across.
TARGETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}

FPS = 30
#: Ceilings. A teaser is short by definition, and these keep one bad storyboard
#: from occupying the machine for an hour.
MAX_SHOTS = 40
MAX_SECONDS = 300.0
MIN_SHOT_SECONDS = 0.4
#: Crossfade length. Long enough to read as a dissolve, short enough that a
#: two-second shot is still mostly that shot.
TRANSITION_SECONDS = 0.5

# ── palette ────────────────────────────────────────────────────────────────
#
# Sampled from the reference teaser rather than chosen: every value below is
# the brightest pixel of that element in a frame of the film this is modelled
# on. The ground is a warm near-black with a soft amber glow behind the title,
# not flat black, and the type is cream rather than white — which is most of
# why a flat-black card with white type reads as a slide and this reads as a
# title sequence.

GROUND = (12, 6, 4)          #: the corners
GLOW = (59, 30, 15)          #: behind the title block, falling off to GROUND
# These are what the reference reads on its *finished* frame, and the
# vignette costs roughly 14% before a frame is finished — so what is drawn is
# set brighter by that much, and what comes out the far end matches.
_VIGNETTE_LOSS = 1.16
KICKER_INK = tuple(min(255, int(c * 1.18)) for c in (188, 176, 171))
TITLE_INK = tuple(min(255, int(c * 1.02)) for c in (233, 219, 208))
SUBTITLE_INK = tuple(min(255, int(c * _VIGNETTE_LOSS)) for c in (230, 208, 176))
RULE_INK = (204, 175, 137)   #: the hairline under the title
CAPTION_INK = (255, 237, 196)
CAPTION_SUB_INK = (204, 197, 190)
MOTE_INK = (232, 196, 126)

#: Where each line of the title block sits, as a fraction of frame height —
#: the baselines measured off the reference rather than derived from leading,
#: so a one-line title lands exactly where its title lands. Extra lines push
#: down from there.
TITLE_TOP = 0.142      #: the kicker's baseline
TITLE_MAIN_Y = 0.205   #: the title's
TITLE_SUB_Y = 0.253    #: the local-language line's
TITLE_RULE_Y = 0.276   #: the hairline

#: Tried in order. The first three are macOS, then Linux, then Windows — this
#: runs on all three and a missing font should degrade to a different typeface,
#: never to a crash.
_FONTS_BOLD = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\segoeuib.ttf",
)
_FONTS_REGULAR = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
)


@dataclass
class Shot:
    """One beat of the teaser: a photo or a slice of a clip, on screen for
    `seconds`, optionally captioned."""

    source: Path
    #: Further photographs to show alongside `source` as a collage. Two or
    #: three together is a beat of its own — a wall of faces where a single
    #: photograph would be one face.
    with_: list[Path] = field(default_factory=list)
    seconds: float = 2.5
    caption: str = ""
    #: The translation, set smaller and letterspaced under the caption.
    subcaption: str = ""
    #: Where to start inside a video clip. Ignored for stills.
    start: float = 0.0
    #: zoom_in | zoom_out | pan_left | pan_right | none. Stills only — a clip
    #: has its own motion and does not want more.
    motion: str = "zoom_in"
    #: auto | cover | blur | card. `auto` crops when the shapes nearly agree
    #: and lays the picture on a blurred bed when they do not. `card` insets
    #: the photograph in a frame on a dark ground, the way a printed photo
    #: sits on a page — which is what a collage always does.
    fill: str = "auto"


@dataclass
class Card:
    """A title or end card: type on a plain ground, no photograph."""

    text: str
    subtitle: str = ""
    #: The small letterspaced line above the title — who is presenting this,
    #: or what season it belongs to.
    kicker: str = ""
    seconds: float = 2.0


@dataclass
class LogoWall:
    """The sponsor card: the title block again, a line naming them, and a grid
    of logos.

    Its own type rather than a kind of Card, because a wall of other people's
    marks has rules a title card does not: each logo keeps its own background
    and its own proportions, and none of them may be cropped. A logo cropped
    to fill a tile is somebody's trademark with a piece missing.
    """

    logos: list[Path] = field(default_factory=list)
    heading: str = "Sponsored by"
    #: The same words in the language the film is in, under the heading.
    subheading: str = ""
    columns: int = 2
    seconds: float = 3.5
    #: The title block above the grid — usually the film's own title again.
    card: "Card | None" = None


@dataclass
class Spec:
    shots: list[Shot] = field(default_factory=list)
    title: Card | None = None
    end_card: Card | None = None
    #: A sponsor wall, shown straight after the title card.
    sponsors: LogoWall | None = None
    music: Path | None = None
    #: Where in the track to start, in seconds. The chorus of a four-minute
    #: song is the part anybody wants under a teaser, and asking somebody to
    #: trim an MP3 before they can use it is asking them to go and find other
    #: software.
    music_start: float = 0.0
    #: How much of it to use. 0 means "as much as the film needs". When it is
    #: set and the storyboard is shorter, the film is not stretched — the
    #: music is simply faded out with it.
    music_seconds: float = 0.0
    aspect: str = "9:16"
    #: crossfade | cut
    transition: str = "crossfade"
    #: Cut on the music. Shot lengths are rounded to whole beats of whatever
    #: the track is playing, so the film changes picture when the music does.
    #: Off when there is no music to cut to.
    sync_to_beat: bool = True
    #: The vignette and the drifting light, laid over the finished film so
    #: every shot shares one atmosphere instead of each carrying its own.
    atmosphere: bool = True
    #: Lean the picture into each beat. Needs music with a pulse to lock to;
    #: ignored without one.
    beat_pulse: bool = True
    #: The sound is the music bed, and only that. Shots are rendered silent so
    #: every intermediate has the same streams — mixing some parts with audio
    #: and some without is what makes a concat produce a file that plays for
    #: three seconds and stops. A clip's own sound is still reachable: pass the
    #: clip itself as `music` and ffmpeg takes its audio track.


@dataclass
class RenderResult:
    path: Path
    seconds: float
    width: int
    height: int
    shots: int
    bytes: int
    #: How much shorter the film is than the section of music asked for.
    #: Dissolves cost half a second each, so a storyboard adding up to sixty
    #: seconds becomes a shorter film, and the caller should know by how much
    #: rather than wondering where the minute went.
    short_by: float = 0.0


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def require_ffmpeg() -> tuple[str, str]:
    """(ffmpeg, ffprobe), or a refusal that says how to install them."""
    ff, probe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ff or not probe:
        raise VideoUnavailable(
            "ffmpeg is not installed on this machine, so video cannot be "
            "rendered here. Install it with `brew install ffmpeg` (macOS), "
            "`winget install ffmpeg` (Windows) or `apt install ffmpeg` "
            "(Linux), then try again."
        )
    return ff, probe


def _run(args: list[str], *, what: str) -> None:
    """One ffmpeg invocation. Never through a shell — every path here came
    from an upload, and a filename is not a place to trust quoting."""
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise RenderError(f"{what} failed: " + " / ".join(tail))


def probe(path: Path) -> dict:
    """Duration, dimensions and whether there is sound. Best effort: an
    unreadable file comes back empty rather than raising, so the caller can
    say which upload was the problem."""
    _, ffprobe = require_ffmpeg()
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001 — a probe failure is not a render failure
        return {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in ((data.get("format") or {}).get("duration"),
                      (video or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    return {
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "has_video": video is not None,
        "has_audio": audio is not None,
        #: A still photograph read by ffmpeg is a one-frame video stream.
        "is_still": bool(video) and (video.get("codec_name") in
                                     {"mjpeg", "png", "webp", "bmp", "gif", "tiff"}),
    }


# ── drawing ────────────────────────────────────────────────────────────────
#
# Type is set through `textkit`, which shapes with HarfBuzz where it can.
# That matters here more than anywhere: a teaser is captioned in whatever
# language the people in it speak, and a caption in Kannada or Hindi drawn
# glyph-by-glyph comes out as broken conjuncts and floating vowel marks.

def _text_layer(size: tuple[int, int]) -> "Image.Image":
    from PIL import Image

    return Image.new("RGBA", size, (0, 0, 0, 0))


def _centred(layer, y: float, text: str, font: str, px: int,
             fill: tuple[int, int, int, int], *, tracking: float = 0.0) -> None:
    width = layer.size[0]
    measured = textkit.measure(text, font, px, tracking=tracking)
    textkit.draw(layer, ((width - measured) / 2, y), text, font, px, fill,
                 tracking=tracking)


def _scrim(layer, top: int, strength: int = 200) -> None:
    """A gradient under the words, because white type on an unknown photograph
    is legible or invisible depending on the photograph."""
    from PIL import ImageDraw

    width, height = layer.size
    draw = ImageDraw.Draw(layer)
    for y in range(max(0, top), height):
        ratio = (y - top) / max(1, height - top)
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, int(strength * ratio ** 1.5)))


def _caption_layer(primary: str, secondary: str,
                   size: tuple[int, int]) -> "Path | None":
    """The caption pair: the line itself, and its translation under it.

    Two lines rather than one because that is how a teaser for a bilingual
    audience is captioned — the sentence in the language it was said in, and
    a quieter translation beneath, set smaller and letterspaced so it reads as
    a subtitle rather than as a second sentence.
    """
    primary, secondary = primary.strip(), secondary.strip()
    if not primary and not secondary:
        return None

    width, height = size
    layer = _text_layer(size)
    margin = width // 11

    main_px = max(34, width // 17)
    main_font = textkit.font_for(primary or secondary, weight="bold")
    sub_px = max(18, width // 44)
    sub_font = textkit.font_for(secondary or primary)
    tracking = sub_px * 0.16

    main_lines = (textkit.wrap(primary, main_font, main_px, width - 2 * margin)[:3]
                  if primary else [])
    sub_lines = (textkit.wrap(secondary, sub_font, sub_px, width - 2 * margin,
                              tracking=tracking)[:2] if secondary else [])

    main_lh = textkit.line_height(main_font, main_px, factor=1.26)
    sub_lh = textkit.line_height(sub_font, sub_px, factor=1.5)
    gap = sub_px if (main_lines and sub_lines) else 0
    block = main_lh * len(main_lines) + gap + sub_lh * len(sub_lines)

    # Sits on the lower third, clear of a phone's own interface furniture.
    baseline = height - int(height * 0.11) - block + main_lh
    _scrim(layer, int(baseline - main_lh - margin))

    y = baseline
    for line in main_lines:
        _centred(layer, y, line, main_font, main_px, (*CAPTION_INK, 255))
        y += main_lh
    y += gap
    for line in sub_lines:
        _centred(layer, y, textkit.uppercase_spaced(line), sub_font, sub_px,
                 (*CAPTION_SUB_INK, 235), tracking=tracking)
        y += sub_lh

    out = Path(tempfile.mkstemp(suffix=".png")[1])
    layer.save(out)
    return out


def _title_block(layer, card: "Card", size: tuple[int, int], top: float) -> float:
    """Kicker, title, local-language line, hairline rule — in that order, from
    `top` down. Returns the y the block ended at, so a sponsor wall knows
    where its grid can start.

    Sizes are fractions of the frame width, taken off the reference: the
    kicker is small and widely letterspaced, the title is the only large
    thing on the card, and the line beneath it is the same words in the
    language the film is in.
    """
    from PIL import ImageDraw

    width, height = size
    # The reference sets its local-language line nearly edge to edge — at
    # width//9 the Kannada subtitle wrapped to two lines, which pushed the
    # rule, the sponsor heading and the whole logo grid down the frame.
    margin = width // 16

    # Sized against the reference frame rather than by eye: its kicker sets a
    # 37px cap height, its title 67px and its local-language line 50px, on a
    # 1080-wide frame. Dividing back out by the ~0.73 cap-height ratio of
    # these faces gives the three divisors below.
    kicker_px = max(24, width // 21)
    title_px = max(60, width // 12)
    sub_px = max(32, width // 16)
    kicker_font = textkit.font_for(card.kicker or "A", weight="regular")
    title_font = textkit.font_for(card.text or "A", weight="display")
    sub_font = textkit.font_for(card.subtitle or "A")
    tracking = kicker_px * 0.34

    if card.kicker.strip():
        _centred(layer, height * top, textkit.uppercase_spaced(card.kicker.strip()),
                 kicker_font, kicker_px, (*KICKER_INK, 255), tracking=tracking)

    # Each group starts at its measured baseline; only a second or third line
    # advances from there, so the common one-line case lands exactly.
    y = height * TITLE_MAIN_Y
    for i, line in enumerate(textkit.wrap(card.text.strip(), title_font, title_px,
                                          width - 2 * margin)[:4]):
        if i:
            y += textkit.line_height(title_font, title_px, factor=1.16)
        _centred(layer, y, line, title_font, title_px, (*TITLE_INK, 255))
    overflow = max(0.0, y - height * TITLE_MAIN_Y)

    if card.subtitle.strip():
        y = height * TITLE_SUB_Y + overflow
        for i, line in enumerate(textkit.wrap(card.subtitle.strip(), sub_font, sub_px,
                                              width - 2 * margin)[:3]):
            if i:
                y += textkit.line_height(sub_font, sub_px, factor=1.32)
            _centred(layer, y, line, sub_font, sub_px, (*SUBTITLE_INK, 255))
        overflow = max(overflow, y - height * TITLE_SUB_Y)

    # The rule sits under the whole block, short and centred.
    y = height * TITLE_RULE_Y + overflow
    rule_w = width // 7
    ImageDraw.Draw(layer).line(
        [((width - rule_w) / 2, y), ((width + rule_w) / 2, y)],
        fill=(*RULE_INK, 225), width=max(2, height // 760))
    return y


def _card_layer(card: Card, size: tuple[int, int]) -> Path:
    """A title card's type, as a transparent layer over its own ground.

    Drawn as a layer rather than onto the background so it can be faded and
    lifted into place by the same overlay that animates a caption — type that
    simply appears is the difference between a title card and a slide.
    """
    layer = _text_layer(size)
    _title_block(layer, card, size, TITLE_TOP)
    out = Path(tempfile.mkstemp(suffix=".png")[1])
    layer.save(out)
    return out


def warm_ground(size: tuple[int, int], *, focus: float = 0.34):
    """The ground every card sits on: warm near-black with a soft amber glow.

    Returns an image rather than a path, because the sponsor wall draws on top
    of it. `focus` is where the glow's centre sits vertically, as a fraction
    of the frame — under the title block, which is where the reference puts
    it.
    """
    from PIL import Image, ImageFilter

    width, height = size
    image = Image.new("RGB", size, GROUND)
    glow = Image.new("RGB", (width // 8, height // 8), GROUND)
    pixels = glow.load()
    gw, gh = glow.size
    cx, cy = gw / 2, gh * focus
    # An ellipse rather than a circle: the frame is twice as tall as it is
    # wide, and a circular glow in it reads as a spotlight.
    radius_x, radius_y = gw * 0.78, gh * 0.68
    for y in range(gh):
        for x in range(gw):
            d = (((x - cx) / radius_x) ** 2 + ((y - cy) / radius_y) ** 2) ** 0.5
            k = max(0.0, 1.0 - d) ** 1.6
            pixels[x, y] = tuple(
                int(GROUND[i] + (GLOW[i] - GROUND[i]) * k) for i in range(3))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=6))
    return Image.blend(image, glow.resize(size, Image.LANCZOS), 1.0)


def _card_ground(size: tuple[int, int]) -> Path:
    """`warm_ground` as a file, for the still pipeline to loop over."""
    out = Path(tempfile.mkstemp(suffix=".png")[1])
    warm_ground(size).save(out)
    return out


def _tile_ground(logo, size: tuple[int, int]) -> tuple[int, int, int]:
    """The colour to fill a logo's tile with — taken from the logo's own
    corners, so a mark drawn for a white background gets a white tile and one
    drawn for a dark background keeps its dark. Transparent corners mean the
    logo was cut out, and those get white."""
    rgba = logo.convert("RGBA")
    w, h = rgba.size
    corners = [rgba.getpixel(p) for p in
               ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    opaque = [c for c in corners if c[3] > 200]
    if not opaque:
        return (255, 255, 255)
    return tuple(sum(c[i] for c in opaque) // len(opaque) for i in range(3))


def compose_logo_wall(wall: LogoWall, size: tuple[int, int]) -> Path:
    """The sponsor card, drawn whole.

    Built with Pillow and handed to the still pipeline as one picture, the
    same bargain `compose_card` makes: a card with eight logos on it is still
    a card.
    """
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    canvas = warm_ground(size)
    layer = _text_layer(size)

    y = height * TITLE_TOP
    if wall.card is not None:
        y = _title_block(layer, wall.card, size, TITLE_TOP)

    head_px = max(22, width // 34)
    sub_px = max(20, width // 40)
    head_font = textkit.font_for(wall.heading or "A", weight="regular")
    if wall.heading.strip():
        y += height * 0.035
        _centred(layer, y, textkit.uppercase_spaced(wall.heading.strip()),
                 head_font, head_px, (*TITLE_INK, 240), tracking=head_px * 0.3)
    if wall.subheading.strip():
        sub_font = textkit.font_for(wall.subheading)
        y += textkit.line_height(sub_font, sub_px, factor=1.5)
        _centred(layer, y, wall.subheading.strip(), sub_font, sub_px,
                 (*SUBTITLE_INK, 235))

    logos = [p for p in wall.logos if p.exists()][:MAX_LOGOS]
    if not logos:
        raise RenderError("a sponsor card needs at least one logo")

    columns = max(1, min(3, wall.columns))
    rows = (len(logos) + columns - 1) // columns
    margin = int(width * 0.065)
    gap = int(width * 0.022)
    top = y + height * 0.045
    tile_w = (width - 2 * margin - gap * (columns - 1)) // columns
    available = height - top - height * 0.05
    tile_h = int(min(tile_w * 0.55, (available - gap * (rows - 1)) / max(1, rows)))
    if tile_h < 40:
        raise RenderError(f"{len(logos)} logos will not fit on one card; "
                          "show fewer, or use two cards")
    radius = max(8, width // 90)

    for index, path in enumerate(logos):
        try:
            logo = Image.open(path)
        except Exception as err:  # noqa: BLE001
            raise RenderError(f"{path.name} could not be opened: {err}") from err
        col, row = index % columns, index // columns
        x = margin + col * (tile_w + gap)
        ty = int(top + row * (tile_h + gap))

        tile = Image.new("RGB", (tile_w, tile_h), _tile_ground(logo, size))
        # Contained, never cropped, with room to breathe around it.
        pad = int(min(tile_w, tile_h) * 0.12)
        fitted = logo.convert("RGBA")
        scale = min((tile_w - 2 * pad) / fitted.width,
                    (tile_h - 2 * pad) / fitted.height)
        fitted = fitted.resize((max(1, int(fitted.width * scale)),
                                max(1, int(fitted.height * scale))), Image.LANCZOS)
        tile.paste(fitted, ((tile_w - fitted.width) // 2,
                            (tile_h - fitted.height) // 2), fitted)

        rounded = _rounded(tile, radius)
        shadow = Image.new("RGBA", (tile_w + 36, tile_h + 36), (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 120), (18, 22, 18 + tile_w, 22 + tile_h))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=10))
        canvas.paste(shadow, (x - 18, ty - 18), shadow)
        canvas.paste(rounded, (x, ty), rounded)

    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(layer)
    out = Path(tempfile.mkstemp(suffix=".png")[1])
    canvas.convert("RGB").save(out)
    return out


#: More than this on one card and every mark is too small to read.
MAX_LOGOS = 12


# ── the music ──────────────────────────────────────────────────────────────

@dataclass
class BeatGrid:
    """Where the beats are: a period, and where the first one falls."""

    bpm: float
    period: float
    offset: float

    def snap(self, seconds: float, *, minimum: float = MIN_SHOT_SECONDS) -> float:
        """`seconds`, rounded to a whole number of beats.

        Rounded rather than floored: a 2.5s shot against a 0.6s beat becomes
        2.4s, not 1.8s, so the storyboard's pacing survives being put on the
        grid. At least one beat, always — a shot shorter than a beat is a
        flash that the cut lands nowhere near.
        """
        beats = max(1, round(seconds / self.period))
        return max(minimum, beats * self.period)


#: The range to look for a beat in — deliberately narrow.
#:
#: Autocorrelation cannot tell a beat from a bar: every pulse at 120 BPM is
#: also a pulse at 60, and the slower one often correlates *better* because it
#: lines up with the phrase. Measured on a real track (a four-minute Ganpati
#: song), the unconstrained search returned 63 BPM — a beat every 0.95s, which
#: rounds every shot to a second and makes a half-second burst impossible.
#: Searching only where dance and film music is actually played returns 100
#: BPM for the same track, which is what it is.
#:
#: A genuinely slow song comes back as its double. That is the right failure:
#: cutting on the half-beat still lands on the music, and cutting on the bar
#: does not read as cutting to it at all.
_MIN_BPM, _MAX_BPM = 90.0, 160.0
#: Loudness is sampled this often. 50ms is finer than any beat and coarse
#: enough that 90 seconds of audio is under two thousand numbers.
_ENVELOPE_HOP = 0.05


def beat_grid(audio: Path, *, start: float = 0.0,
              duration: float = 0.0) -> BeatGrid | None:
    """Work out the tempo of a track, and where its beats land.

    Loudness over time is read out of ffmpeg — no numpy, no analysis library,
    because pulling either in for one autocorrelation is not a trade worth
    making. Rises in loudness are the onsets; the lag that best correlates the
    onsets with themselves is the beat; the phase that best lines a pulse
    train up with the onsets is where the first beat falls.

    Returns None when it cannot hear a pulse — silence, speech, a drone — and
    a caller that gets None should cut to its own pacing rather than to a
    number this function guessed.
    """
    _, _ = require_ffmpeg()
    envelope = _loudness_envelope(audio, start=start, duration=duration)
    if len(envelope) < 40:
        return None
    onsets = [max(0.0, b - a) for a, b in zip(envelope, envelope[1:])]
    if sum(onsets) <= 0:
        return None

    best_lag, best_score = 0, 0.0
    for lag in range(int(60 / _MAX_BPM / _ENVELOPE_HOP),
                     int(60 / _MIN_BPM / _ENVELOPE_HOP) + 1):
        if lag < 1 or lag >= len(onsets):
            continue
        score = sum(onsets[i] * onsets[i + lag] for i in range(len(onsets) - lag))
        score /= (len(onsets) - lag)
        if score > best_score:
            best_lag, best_score = lag, score
    if not best_lag:
        return None


    # Phase: slide a pulse train across the onsets and keep the offset whose
    # pulses sit on the loudest rises.
    best_phase, phase_score = 0, -1.0
    for phase in range(best_lag):
        score = sum(onsets[i] for i in range(phase, len(onsets), best_lag))
        if score > phase_score:
            best_phase, phase_score = phase, score

    period = best_lag * _ENVELOPE_HOP
    return BeatGrid(bpm=round(60 / period, 1), period=period,
                    offset=round(best_phase * _ENVELOPE_HOP, 3))


def _loudness_envelope(audio: Path, *, start: float = 0.0,
                       duration: float = 0.0) -> list[float]:
    """RMS level per window, in dB, straight out of ffmpeg's `astats`.

    `start` and `duration` narrow it to one section, because the tempo that
    matters is the tempo of the part being used — a song with a slow opening
    and a fast chorus has two, and averaging them gives neither.
    """
    ff, _ = require_ffmpeg()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
        report = Path(handle.name)
    try:
        window = (["-ss", f"{start:.3f}"] if start > 0 else []) + \
                 (["-t", f"{duration:.3f}"] if duration > 0 else [])
        subprocess.run(
            [ff, "-v", "quiet", *window, "-i", str(audio),
             "-af", f"asetnsamples={int(44100 * _ENVELOPE_HOP)},"
                    "astats=metadata=1:reset=1,"
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level:"
                    f"file={report}",
             "-f", "null", "-"],
            capture_output=True, timeout=180,
        )
        values: list[float] = []
        for line in report.read_text(errors="replace").splitlines():
            if "RMS_level=" in line:
                raw = line.split("=", 1)[1].strip()
                # Digital silence reports -inf, which no arithmetic survives.
                values.append(-90.0 if raw.lstrip("-") in {"inf", "nan"} else float(raw))
        return values
    except Exception:  # noqa: BLE001 — no pulse found is a valid answer
        logger.warning("could not read the loudness of %s", audio.name)
        return []
    finally:
        report.unlink(missing_ok=True)


# ── composition ────────────────────────────────────────────────────────────

#: The ground a framed photograph sits on, and the frame around it.
_CARD_GROUND = (12, 11, 14)
_CARD_EDGE = (238, 234, 226)


def _rounded(image, radius: int):
    """`image` with its corners rounded off, as RGBA."""
    from PIL import Image, ImageDraw

    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (image.size[0] - 1, image.size[1] - 1)],
                                           radius=radius, fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask)
    return out


def _fit(image, box: tuple[int, int]):
    """Cover `box` and crop the overflow — a frame is a crop, not a letterbox."""
    from PIL import Image

    target_w, target_h = box
    ratio = max(target_w / image.width, target_h / image.height)
    scaled = image.resize((max(1, int(image.width * ratio)),
                           max(1, int(image.height * ratio))), Image.LANCZOS)
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


def _justified_row(aspects: list[float], width: int, max_height: int,
                   gap: int) -> list[tuple[int, int]]:
    """Sizes for a row of pictures that keeps every one of their shapes.

    The classic justified gallery: give every picture the same height, let its
    width follow its own aspect, and choose the height that makes the row fill
    the width exactly. Nothing is cropped, which is the whole point — a
    portrait photograph forced into a landscape tile lost 68% of its height,
    and what it lost was people.
    """
    usable = width - gap * (len(aspects) - 1)
    height = min(max_height, usable / max(0.01, sum(aspects)))
    return [(max(1, int(height * a)), max(1, int(height))) for a in aspects]


def compose_card(sources: list[Path], size: tuple[int, int]) -> Path:
    """One, two or three photographs framed on a dark ground.

    Built with Pillow and handed to the ordinary still pipeline as a picture,
    so a collage needs no new path through ffmpeg: it is a photograph that
    happens to have been assembled first.

    The frame is the point. A photograph shown full-bleed is the film; a
    photograph shown framed, with the ground visible around it, reads as a
    photograph being shown to you — which is what a montage of somebody's
    festival pictures wants.

    Portraits stand side by side and landscapes stack, because that is the
    arrangement that leaves each of them whole.
    """
    from PIL import Image, ImageFilter

    width, height = size
    photos: list = []
    for path in [p for p in sources if p.exists()][:3]:
        try:
            photos.append((path, Image.open(path).convert("RGB")))
        except Exception as err:  # noqa: BLE001
            raise RenderError(f"{path.name} could not be opened: {err}") from err
    if not photos:
        raise RenderError("a collage needs at least one photograph")

    canvas = Image.new("RGB", size, _CARD_GROUND)
    try:
        bed = _fit(photos[0][1], size)
        bed = bed.filter(ImageFilter.GaussianBlur(radius=max(18, width // 26)))
        canvas = Image.blend(Image.new("RGB", size, _CARD_GROUND), bed, 0.38)
    except Exception:  # noqa: BLE001 — a flat ground is an acceptable fallback
        pass

    aspects = [im.width / im.height for _, im in photos]
    upright = sum(1 for a in aspects if a < 1.0) > len(aspects) / 2
    margin = int(width * 0.075)
    gap = int(width * 0.022)
    border = max(3, width // 240)
    radius = max(8, width // 70)
    inner_w = width - 2 * margin
    # Room for the caption to sit under it without covering anything.
    inner_h = int(height * 0.62)

    if len(photos) == 1 or upright:
        # A row: portraits beside each other, each keeping its full height.
        sizes = _justified_row(aspects, inner_w - 2 * border * len(photos),
                               inner_h, gap)
        row_w = sum(w for w, _ in sizes) + gap * (len(sizes) - 1) + 2 * border * len(sizes)
        row_h = max(h for _, h in sizes)
        x = (width - row_w) // 2
        placements = []
        for (w, h) in sizes:
            placements.append((x, int(height * 0.5 - row_h / 2), w, h))
            x += w + gap + 2 * border
    else:
        # A column: landscapes stacked, each keeping its full width.
        stack_w = min(inner_w, int(inner_h / max(0.01, sum(1 / a for a in aspects))
                                   * len(aspects)))
        stack_w = min(inner_w, stack_w)
        heights = [int(stack_w / a) for a in aspects]
        total = sum(heights) + gap * (len(heights) - 1) + 2 * border * len(heights)
        if total > inner_h:  # shrink to fit rather than crop
            scale = inner_h / total
            stack_w = int(stack_w * scale)
            heights = [int(h * scale) for h in heights]
            total = sum(heights) + gap * (len(heights) - 1) + 2 * border * len(heights)
        y = int(height * 0.5 - total / 2)
        placements = []
        for h in heights:
            placements.append(((width - stack_w) // 2, y, stack_w, h))
            y += h + gap + 2 * border

    for (path, photo), (x, y, box_w, box_h) in zip(photos, placements):
        # Resized, never cropped: the box was built from this photo's own
        # proportions, so `resize` is all that is needed.
        fitted = photo.resize((max(1, box_w), max(1, box_h)), Image.LANCZOS)
        framed = Image.new("RGB", (box_w + 2 * border, box_h + 2 * border), _CARD_EDGE)
        framed.paste(fitted, (border, border))
        rounded = _rounded(framed, radius)
        shadow = Image.new("RGBA", (rounded.width + 40, rounded.height + 40), (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 150), (20, 26, 20 + rounded.width, 26 + rounded.height))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=14))
        canvas.paste(shadow, (x - 20, y - 20), shadow)
        canvas.paste(rounded, (x, y), rounded)

    out = Path(tempfile.mkstemp(suffix=".png")[1])
    canvas.save(out)
    return out


# ── shots ──────────────────────────────────────────────────────────────────

def _cover(width: int, height: int) -> str:
    """Fill the frame and crop the overflow, rather than letterboxing.

    A phone photo in a 9:16 teaser should fill the screen; black bars read as
    a mistake. `increase` scales until both axes are covered, then the crop
    takes the middle.
    """
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}")


#: How much of a photograph may be thrown away to fill the frame.
#:
#: Stated as lost picture rather than as a difference of ratios, because that
#: is the thing anybody actually minds: a 3:4 photo held upright loses a
#: quarter of its width to a 9:16 frame, and a quarter of the width of a group
#: photograph is people. Past this, the whole picture is shown on a blurred
#: bed of itself instead.
MAX_CROP_LOSS = 0.12


def _crop_loss(src: tuple[int, int], size: tuple[int, int]) -> float:
    """The fraction of the picture a cover-crop would discard."""
    src_w, src_h = src
    if not src_w or not src_h:
        return 0.0
    source = src_w / src_h
    target = size[0] / size[1]
    kept = target / source if source > target else source / target
    return max(0.0, 1.0 - kept)


def _wants_blur_bed(fill: str, src: tuple[int, int], size: tuple[int, int]) -> bool:
    if fill == "cover":
        return False
    if fill == "blur":
        return True
    return _crop_loss(src, size) > MAX_CROP_LOSS


def _blur_bed(width: int, height: int, tag: str) -> str:
    """The whole picture, centred, over a blurred enlargement of itself.

    What every phone app does with a photograph that is the wrong shape, and
    for the same reason: nothing is cut off, and the frame is still full. The
    bed is darkened so the photograph in front of it stays the subject.
    """
    return (f"split[{tag}bg][{tag}fg];"
            f"[{tag}bg]{_cover(width, height)},boxblur=luma_radius={max(8, width // 36)}:"
            f"luma_power=2,eq=brightness=-0.16:saturation=0.9[{tag}bed];"
            f"[{tag}fg]scale={width}:{height}:force_original_aspect_ratio=decrease[{tag}pic];"
            f"[{tag}bed][{tag}pic]overlay=(W-w)/2:(H-h)/2")


#: How hard the picture leans into a beat, as a fraction of frame size. Small
#: on purpose: this should be felt rather than watched, and a photograph that
#: visibly jumps every half second is a music video, not a teaser.
BEAT_PULSE = 0.022
#: The punch decays over this fraction of a beat.
_PULSE_DECAY = 0.34


def _pulse_term(pulse: "tuple[float, float] | None") -> str:
    """An expression that adds a small push on every beat, or "" for none.

    `pulse` is (beat period in seconds, phase in seconds). The phase is what
    keeps the push on the music rather than merely regular: the film starts
    wherever the storyboard starts, and the track's first beat inside the
    section being used is rarely at exactly that moment.
    """
    if not pulse:
        return ""
    period, phase = pulse
    beat_frames = max(2.0, period * FPS)
    decay = max(1.0, beat_frames * _PULSE_DECAY)
    offset = (phase % period) * FPS
    return (f"+{BEAT_PULSE}*max(0,1-mod(on+{offset:.2f},{beat_frames:.2f})"
            f"/{decay:.2f})")


def _zoompan(motion: str, seconds: float, width: int, height: int,
             pulse: "tuple[float, float] | None" = None) -> str:
    """Ken Burns. The still is scaled up first because `zoompan` samples from
    the input frame: zooming a 1080-wide source to 1.1 means inventing pixels,
    and the drift crawls. Sampling a 2x source keeps it smooth."""
    frames = max(1, int(round(seconds * FPS)))
    # 4% per second, so a long shot does not end up on someone's nostril.
    span = min(0.35, 0.04 * seconds)
    if motion == "zoom_out":
        z = f"if(eq(on,0),{1 + span},max(zoom-{span / frames:.8f},1.0))"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "pan_left":
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        z, x, y = "1.12", f"(iw-iw/zoom)*(on/{frames})", "ih/2-(ih/zoom/2)"
    elif motion == "none":
        z, x, y = "1.0", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:  # zoom_in, and anything unrecognised
        z = f"min(zoom+{span / frames:.8f},{1 + span})"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return (f"zoompan=z='{z}{_pulse_term(pulse)}':x='{x}':y='{y}':"
            f"d={frames}:s={width}x{height}:fps={FPS}")


#: How the type arrives: a pause, then half a second of fading up while it
#: lifts the last few pixels into place. Text that simply appears is the
#: difference between a title card and a slide.
TEXT_DELAY = 0.18
TEXT_FADE = 0.55
TEXT_RISE_PX = 26


def _animated_overlay(layer_index: int, seconds: float) -> str:
    """Filter chain that fades a text layer up and lifts it into place.

    Expressed as filters rather than as redrawn frames: ffmpeg is already
    decoding every frame, and asking Pillow for thirty layers a second to
    move some type 26 pixels would cost more than the whole render.
    """
    hold = min(TEXT_DELAY, max(0.0, seconds - 0.2))
    fade = min(TEXT_FADE, max(0.2, seconds - hold))
    settle = hold + fade
    # y drops from TEXT_RISE_PX to 0 across the fade, so the words arrive
    # from slightly below where they come to rest.
    rise = rf"max(0\,({settle:.3f}-t))/{fade:.3f}*{TEXT_RISE_PX}"
    return (f"[{layer_index}:v]format=rgba,"
            f"fade=t=in:st={hold:.3f}:d={fade:.3f}:alpha=1[txt];"
            f"[bg][txt]overlay=0:'{rise}'[v]")


def _render_still(ff: str, image: Path, shot: Shot, size: tuple[int, int],
                  out: Path, *, source_size: tuple[int, int] = (0, 0),
                  layer: Path | None = None,
                  pulse: "tuple[float, float] | None" = None) -> None:
    width, height = size
    own_layer = layer is None
    if own_layer:
        layer = _caption_layer(shot.caption, shot.subcaption, size)
    # Everything upstream of zoompan is built at twice the frame, because
    # zoompan samples from the frame it is handed: zooming a 1080-wide source
    # to 1.1 invents pixels and the drift visibly crawls. A 2x bed is smooth.
    if _wants_blur_bed(shot.fill, source_size, size):
        fit = _blur_bed(width * 2, height * 2, "s")
    else:
        fit = _cover(width * 2, height * 2)
    motion = _zoompan(shot.motion, shot.seconds, width, height, pulse)
    chain = f"[0:v]{fit},{motion},setsar=1"

    # `-t` goes on the OUTPUT, not the input. `zoompan` expands each frame it
    # is given into `d` frames, so bounding the looped input first hands it 75
    # frames and gets 75 pans back — a two-second shot that runs for a minute.
    # Loop the still indefinitely, let zoompan produce exactly one pan, and cut
    # the output at the shot length.
    args = [ff, "-y", "-loop", "1", "-i", str(image)]
    if layer:
        # `-loop 1` on the text too. A PNG is one frame at t=0; `fade` leaves
        # that frame fully transparent (the fade-in has not started yet) and
        # `overlay` then repeats the transparent frame for the whole shot —
        # which is how animating the type made it disappear altogether.
        args += ["-loop", "1", "-framerate", str(FPS),
                 "-t", f"{shot.seconds:.3f}", "-i", str(layer),
                 "-filter_complex",
                 f"{chain}[bg];{_animated_overlay(1, shot.seconds)}", "-map", "[v]"]
    else:
        args += ["-filter_complex", f"{chain}[v]", "-map", "[v]"]
    args += ["-t", f"{shot.seconds:.3f}"] + _VIDEO_ENCODE + [str(out)]
    try:
        _run(args, what=f"rendering {image.name}")
    finally:
        if own_layer and layer:
            layer.unlink(missing_ok=True)


def _render_clip(ff: str, clip: Path, shot: Shot, size: tuple[int, int],
                 out: Path, *, source_size: tuple[int, int] = (0, 0)) -> None:
    width, height = size
    layer = _caption_layer(shot.caption, shot.subcaption, size)
    if _wants_blur_bed(shot.fill, source_size, size):
        fit = _blur_bed(width, height, "c")
    else:
        fit = _cover(width, height)
    chain = f"[0:v]{fit},fps={FPS},setsar=1"
    # -ss before -i seeks by keyframe, which is fast and accurate enough for a
    # teaser; -t after it bounds the slice.
    args = [ff, "-y", "-ss", f"{max(0.0, shot.start):.3f}", "-t", f"{shot.seconds:.3f}",
            "-i", str(clip)]
    if layer:
        args += ["-loop", "1", "-framerate", str(FPS),
                 "-t", f"{shot.seconds:.3f}", "-i", str(layer),
                 "-filter_complex",
                 f"{chain}[bg];{_animated_overlay(1, shot.seconds)}", "-map", "[v]"]
    else:
        args += ["-filter_complex", f"{chain}[v]", "-map", "[v]"]
    args += ["-an"] + _VIDEO_ENCODE + [str(out)]
    try:
        _run(args, what=f"rendering {clip.name}")
    finally:
        if layer:
            layer.unlink(missing_ok=True)


# ── atmosphere ─────────────────────────────────────────────────────────────

#: A loop of drifting light, laid over the finished film. Three seconds is
#: long enough that the eye does not catch the repeat and short enough to
#: render in a moment.
_PARTICLE_SECONDS = 3.0
_PARTICLE_COUNT = 90


def _particle_loop(ff: str, size: tuple[int, int], work: Path) -> Path:
    """A seamless loop of warm motes rising through the frame.

    Drawn at half size and scaled up by ffmpeg — every mote is a blurred disc,
    so there is no detail in it that surviving a 2x scale would preserve, and
    ninety frames at full size costs four times as much for a picture nobody
    can tell apart.

    Seamless because each mote's position is a function of `frame / total`
    wrapped into 0-1: at the last frame every mote is exactly where it was at
    the first.
    """
    import math
    import random

    from PIL import Image, ImageDraw, ImageFilter

    width, height = size[0] // 2, size[1] // 2
    frames = int(_PARTICLE_SECONDS * FPS)
    # Seeded, so re-rendering the same storyboard gives the same film.
    rng = random.Random(20260922)
    motes = [(rng.random(), rng.random(), rng.uniform(0.35, 1.0),
              rng.uniform(0.5, 1.6), rng.uniform(0, math.tau))
             for _ in range(_PARTICLE_COUNT)]

    folder = work / "motes"
    folder.mkdir(exist_ok=True)
    for index in range(frames):
        phase = index / frames
        frame = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(frame)
        for x0, y0, brightness, speed, wobble in motes:
            y = (y0 - phase * speed) % 1.0
            x = (x0 + 0.01 * math.sin(math.tau * phase + wobble)) % 1.0
            radius = 1.0 + 3.2 * brightness
            twinkle = 0.55 + 0.45 * math.sin(math.tau * (phase * 2 + wobble))
            level = brightness * twinkle
            draw.ellipse([(x * width - radius, y * height - radius),
                          (x * width + radius, y * height + radius)],
                         fill=tuple(int(c * level) for c in MOTE_INK))
        frame.filter(ImageFilter.GaussianBlur(radius=1.4)).save(
            folder / f"{index:04d}.png")

    loop = work / "motes.mp4"
    _run([ff, "-y", "-framerate", str(FPS), "-i", str(folder / "%04d.png"),
          "-vf", f"scale={size[0]}:{size[1]}", *_VIDEO_ENCODE, str(loop)],
         what="drawing the drifting light")
    return loop


def _apply_atmosphere(ff: str, video: Path, out: Path, size: tuple[int, int],
                      seconds: float, work: Path) -> None:
    """Vignette and drifting light over the whole film.

    Applied once at the end rather than per shot, so every photograph, clip
    and title card sits in the same air. `screen` because the loop is black
    everywhere except the motes, and black under `screen` changes nothing —
    no alpha channel needed, and nothing to go wrong where the loop is empty.
    """
    loop = _particle_loop(ff, size, work)
    _run([ff, "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(loop),
          "-filter_complex",
          # In RGB, deliberately. `blend` works plane by plane, and in YUV
          # the planes it would screen are chroma centred on 128 — screening
          # those drives every frame towards magenta, which is exactly what
          # the first render of this did.
          # PI/5 rather than PI/4.2: the stronger vignette took 18% off the
          # title's brightness, which is measurable as the difference between
          # the reference's (233,219,208) and a washed-out (192,187,162).
          "[0:v]vignette=angle=PI/6:mode=forward,format=gbrp[vig];"
          "[1:v]format=gbrp,colorchannelmixer=rr=0.6:gg=0.6:bb=0.6[mo];"
          "[vig][mo]blend=all_mode=screen:shortest=1,format=yuv420p[v]",
          "-map", "[v]", "-map", "0:a?", "-t", f"{seconds:.3f}",
          *_VIDEO_ENCODE, "-c:a", "copy", str(out)],
         what="laying the light over it")


#: One encoding profile everywhere, so the assembly pass concatenates streams
#: that already agree. yuv420p and High profile because anything else is a
#: video that plays in ffmpeg and not in a browser.
_VIDEO_ENCODE = [
    "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-preset", "medium", "-crf", "20", "-r", str(FPS),
]


def _assemble(ff: str, parts: list[Path], out: Path, *, transition: str) -> float:
    """Join the shots, and return how long the result runs.

    A cut is a concat. A crossfade is a chain of `xfade`s whose offsets are
    cumulative: each transition overlaps the pair by TRANSITION_SECONDS, so
    every dissolve shortens the film by that much.
    """
    durations = [probe(p).get("duration") or 0.0 for p in parts]
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
        return durations[0]

    if transition == "cut":
        listing = out.parent / "parts.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        _run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-c", "copy", str(out)], what="joining the shots")
        return sum(durations)

    args: list[str] = [ff, "-y"]
    for part in parts:
        args += ["-i", str(part)]
    steps: list[str] = []
    label = "0:v"
    offset = 0.0
    for index in range(1, len(parts)):
        # The dissolve starts TRANSITION_SECONDS before the running total,
        # which is itself net of every dissolve already spent.
        offset += durations[index - 1] - TRANSITION_SECONDS
        nxt = f"x{index}"
        steps.append(f"[{label}][{index}:v]xfade=transition=fade:"
                     f"duration={TRANSITION_SECONDS}:offset={max(0.0, offset):.3f}[{nxt}]")
        label = nxt
    args += ["-filter_complex", ";".join(steps), "-map", f"[{label}]"]
    args += _VIDEO_ENCODE + ["-an", str(out)]
    _run(args, what="dissolving between the shots")
    return sum(durations) - TRANSITION_SECONDS * (len(parts) - 1)


def _add_music(ff: str, video: Path, music: Path, out: Path, seconds: float,
               *, start: float = 0.0) -> None:
    """The music bed, cut to length and faded at both ends.

    Trimmed rather than looped: a teaser that restarts its own soundtrack
    halfway through sounds broken. If the track is shorter than the film, it
    simply ends, and `-shortest` is not used so the picture still runs to the
    end.

    `start` takes the bed from partway into the track — the chorus, usually.
    The seek goes before `-i`, so ffmpeg jumps there rather than decoding two
    minutes of audio it will throw away.
    """
    fade_out_at = max(0.0, seconds - 1.2)
    chain = (f"atrim=0:{seconds:.3f},asetpts=PTS-STARTPTS,"
             f"afade=t=in:st=0:d=0.6,afade=t=out:st={fade_out_at:.3f}:d=1.2,"
             f"aresample=48000")
    seek = ["-ss", f"{start:.3f}"] if start > 0 else []
    _run([ff, "-y", "-i", str(video), *seek, "-i", str(music),
          "-filter_complex", f"[1:a]{chain}[a]",
          "-map", "0:v", "-map", "[a]", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-ac", "2",
          "-movflags", "+faststart", str(out)], what="laying the music under it")


def render(spec: Spec, out_path: Path,
           on_progress: Callable[[str], None] | None = None) -> RenderResult:
    """Render `spec` to `out_path`. Synchronous — call it off the event loop
    with `render_async`, which is what every caller here actually wants."""
    ff, _ = require_ffmpeg()
    size = TARGETS.get(spec.aspect, TARGETS["9:16"])
    if not spec.shots and not spec.title:
        raise RenderError("there is nothing to render — no shots and no title card")
    if len(spec.shots) > MAX_SHOTS:
        raise RenderError(f"{len(spec.shots)} shots is more than a teaser can be; "
                          f"the limit is {MAX_SHOTS}")

    def say(message: str) -> None:
        if on_progress:
            on_progress(message)

    # The music decides the pacing before a single frame is drawn, because
    # every shot length below is rounded to its beat.
    grid: BeatGrid | None = None
    if spec.sync_to_beat and spec.music and spec.music.exists():
        say("listening to the music")
        grid = beat_grid(spec.music, start=spec.music_start,
                         duration=spec.music_seconds)
        if grid:
            say(f"{grid.bpm:g} BPM — cutting on the beat ({grid.period:.2f}s)")
        else:
            say("no steady pulse in the track; cutting to the storyboard instead")

    def beat(seconds: float) -> float:
        return grid.snap(seconds) if grid else max(MIN_SHOT_SECONDS, seconds)

    # One phase for the whole film: every shot is a whole number of beats
    # long, so the offset into the beat never drifts from shot to shot.
    pulse = (grid.period, grid.offset) if (grid and spec.beat_pulse) else None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compass-teaser-") as tmp:
        work = Path(tmp)
        parts: list[Path] = []

        def add_card(card: Card, tag: str) -> None:
            ground = _card_ground(size)
            layer = _card_layer(card, size)
            part = work / f"{tag}.mp4"
            # A card holds still and lets its type arrive; a card that also
            # drifts is two things moving at once for no reason.
            _render_still(ff, ground,
                          Shot(source=ground, seconds=beat(card.seconds),
                               motion="none"),
                          size, part, layer=layer)
            ground.unlink(missing_ok=True)
            layer.unlink(missing_ok=True)
            parts.append(part)

        total_planned = (len(spec.shots) + bool(spec.title) + bool(spec.end_card)
                         + bool(spec.sponsors))
        if spec.title:
            say(f"title card (1 of {total_planned})")
            add_card(spec.title, "title")

        if spec.sponsors:
            say(f"sponsor card ({1 + bool(spec.title)} of {total_planned})")
            wall = compose_logo_wall(spec.sponsors, size)
            part = work / "sponsors.mp4"
            # Held still: a wall of marks is read, not panned across.
            _render_still(ff, wall,
                          Shot(source=wall, seconds=beat(spec.sponsors.seconds),
                               motion="none", fill="cover"),
                          size, part, source_size=size)
            wall.unlink(missing_ok=True)
            parts.append(part)

        for index, shot in enumerate(spec.shots, start=1):
            if not shot.source.exists():
                raise RenderError(f"{shot.source.name} is not there any more")
            seconds = beat(max(MIN_SHOT_SECONDS, float(shot.seconds)))
            info = probe(shot.source)
            part = work / f"shot{index:03d}.mp4"
            step = index + (1 if spec.title else 0) + (1 if spec.sponsors else 0)
            is_clip = bool(info.get("has_video")) and not info.get("is_still")

            # A collage, or a single photograph asked to be framed, is
            # assembled into one picture first and then treated as a still.
            # Clips are never framed: a moving image in a frame on a dark
            # ground is a television, not a teaser.
            composed: Path | None = None
            if not is_clip and (shot.with_ or shot.fill == "card"):
                names = ", ".join(p.name for p in [shot.source, *shot.with_][:3])
                say(f"framing {names} ({step} of {total_planned})")
                composed = compose_card([shot.source, *shot.with_], size)
            else:
                say(f"{shot.source.name} ({step} of {total_planned})")

            if is_clip:
                duration = info.get("duration") or 0.0
                # A start past the end of the clip used to render a file with
                # no frames in it, and the assembly pass then died on a
                # filtergraph referring to a stream that was not there — an
                # ffmpeg message about stream specifiers, for what is really
                # "you asked for the ninth second of a six-second clip".
                # Clamped back to the last usable moment instead.
                start = max(0.0, min(shot.start, max(0.0, duration - MIN_SHOT_SECONDS)))
                available = max(0.0, duration - start)
                if available > 0:
                    seconds = min(seconds, available)
                _render_clip(ff, shot.source,
                             Shot(**{**shot.__dict__, "seconds": seconds,
                                     "start": start}), size, part,
                             source_size=(info.get("width", 0), info.get("height", 0)))
            elif composed is not None:
                # The composition already fills the frame exactly, so it is
                # shown as it was built rather than cropped again.
                _render_still(ff, composed,
                              Shot(**{**shot.__dict__, "seconds": seconds,
                                      "fill": "cover", "motion": shot.motion}),
                              size, part, source_size=size, pulse=pulse)
                composed.unlink(missing_ok=True)
            else:
                _render_still(ff, shot.source,
                              Shot(**{**shot.__dict__, "seconds": seconds}), size, part,
                              source_size=(info.get("width", 0), info.get("height", 0)),
                              pulse=pulse)
            # Checked here, where the shot that produced it is still known.
            # Downstream, an empty part is just an input index.
            if not probe(part).get("has_video"):
                raise RenderError(
                    f"shot {index} ({shot.source.name}) rendered nothing — "
                    "check the start time and length asked of it")
            parts.append(part)

        if spec.end_card:
            say(f"end card ({total_planned} of {total_planned})")
            add_card(spec.end_card, "end")

        say("joining the shots")
        joined = work / "joined.mp4"
        seconds = _assemble(ff, parts, joined, transition=spec.transition)
        if seconds > MAX_SECONDS:
            raise RenderError(f"the storyboard runs {seconds:.0f}s, longer than the "
                              f"{MAX_SECONDS:.0f}s a teaser is allowed to be")

        if spec.atmosphere:
            say("vignette and drifting light")
            lit = work / "lit.mp4"
            _apply_atmosphere(ff, joined, lit, size, seconds, work)
            joined = lit

        if spec.music and spec.music.exists():
            say("laying the music under it")
            _add_music(ff, joined, spec.music, out_path, seconds,
                       start=spec.music_start)
        else:
            # Still a final pass: +faststart moves the index to the front so
            # the file starts playing before it has finished downloading.
            _run([ff, "-y", "-i", str(joined), "-c", "copy",
                  "-movflags", "+faststart", str(out_path)],
                 what="finishing the file")

    short_by = round(max(0.0, spec.music_seconds - seconds), 1) if spec.music_seconds else 0.0
    return RenderResult(path=out_path, seconds=round(seconds, 2),
                        width=size[0], height=size[1], shots=len(parts),
                        bytes=out_path.stat().st_size, short_by=short_by)


async def render_async(spec: Spec, out_path: Path,
                       on_progress: Callable[[str], None] | None = None) -> RenderResult:
    """`render` on a worker thread, so a two-minute encode does not stop the
    server answering anything else while it runs."""
    loop = asyncio.get_running_loop()

    def progress(message: str) -> None:
        if on_progress:
            loop.call_soon_threadsafe(on_progress, message)

    return await asyncio.to_thread(render, spec, out_path, progress)
