"""`make_video` — Home's third tool, and the first one that produces a file.

The other two read: `memory` remembers, `web_fetch` fetches. This one writes,
which is a real change to what Home is, so the boundary is drawn tightly:

  * it reads only files that were uploaded to *this conversation*, resolved by
    `home.media`, which refuses anything outside that directory;
  * it writes only into that same directory;
  * it takes no paths, no commands and no code — a storyboard of ids, seconds
    and captions is the entire vocabulary.

So Home still cannot read your files or run your commands. It can now make a
film out of what you handed it, which is the thing people were asking for and
being told to go to the Agent Console for.

The tool does no rendering itself; `compass.common.video` does, and knows
nothing about chat. This file is the translation layer: ids to paths, a model's
storyboard to a Spec, a finished file to a sentence and a link.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from pydantic import BaseModel, Field

from compass.common import textkit, video
from compass.common.attachments import media_kind
from compass.common.policy.permissions import Behavior, PermissionDecision
from compass.common.tools.base import (
    Progress, Tool, ToolOutput, ToolUseContext, ToolYield)
from compass.home import media

logger = logging.getLogger("compass.home.video")


class ShotIn(BaseModel):
    media: str = Field(
        description="Which upload this shot shows — the id from the list of "
                    "attached media, e.g. '0002-beach.jpg'.")
    seconds: float = Field(
        default=2.5,
        description="How long it is on screen. 1.5–3 is the usual range; a "
                    "shot under 1.2 reads as a flash.")
    caption: str = Field(
        default="",
        description="A line burned along the bottom, centred. Write it in the "
                    "language the people in the video speak — Kannada, Hindi, "
                    "Tamil and the rest are set properly, not transliterated. "
                    "Leave empty for no caption.")
    subcaption: str = Field(
        default="",
        description="The translation, set smaller and letterspaced under the "
                    "caption. This is the pairing a bilingual teaser uses: the "
                    "sentence as it was said, and its English underneath.")
    with_media: list[str] = Field(
        default_factory=list,
        description="Up to two more photo ids to show alongside `media` as a "
                    "framed collage on one card. A wall of faces where a "
                    "single photograph would be one face. Photos only.")
    start: float = Field(
        default=0.0,
        description="For a video clip, how far into it this shot starts. "
                    "Ignored for photographs.")
    motion: str = Field(
        default="zoom_in",
        description="zoom_in | zoom_out | pan_left | pan_right | none. The "
                    "slow drift over a photograph — vary it between shots, "
                    "because the same move on every shot looks mechanical. "
                    "Ignored for video clips, which have their own motion.")
    fill: str = Field(
        default="auto",
        description="auto | cover | blur | card. Use 'auto' unless you have a "
                    "specific reason not to: it fills the frame when the "
                    "photo nearly fits it and lays the whole picture on a "
                    "blurred bed of itself when it does not, which is what "
                    "keeps a landscape photo in a vertical teaser from losing "
                    "half of what is in it. 'cover' always fills and always "
                    "crops; 'blur' always shows the whole picture.")


class MakeVideoInput(BaseModel):
    shots: list[ShotIn] = Field(
        description="The storyboard, in order. Every shot names an upload.")
    kicker: str = Field(
        default="",
        description="The small letterspaced line above the title — who is "
                    "presenting this, or what season it belongs to.")
    title: str = Field(
        default="",
        description="Opening card. A few words, not a sentence.")
    subtitle: str = Field(
        default="",
        description="Smaller line under the title, under a hairline rule. "
                    "Often the title again in the local language.")
    end_text: str = Field(
        default="", description="Closing card, if the teaser wants one.")
    sponsor_logos: list[str] = Field(
        default_factory=list,
        description="Ids of uploaded logo images to show on a sponsor card "
                    "after the title. Each keeps its own background and is "
                    "never cropped. Up to 12; two columns reads best.")
    sponsor_heading: str = Field(
        default="Sponsored by",
        description="The line above the logo grid.")
    sponsor_subheading: str = Field(
        default="",
        description="The same words in the language of the film, under it.")
    music: str = Field(
        default="",
        description="Id of an uploaded audio file to lay underneath. It is "
                    "trimmed to length and faded at both ends. A video clip's "
                    "id works too — its audio track is used.")
    music_start: str = Field(
        default="",
        description="Where in the track to start — \"1:45\", \"1:45.5\" or a "
                    "number of seconds. Use it when the user names a part of "
                    "the song; never ask them to trim the file themselves.")
    music_end: str = Field(
        default="",
        description="Where to stop, same format. With a start, this sets how "
                    "long the music runs, and the storyboard should be about "
                    "that long so the film and the music end together.")
    beat_pulse: bool = Field(
        default=True,
        description="Lean the picture into each beat — a small push on every "
                    "beat, phase-locked to the track. This is what makes "
                    "photos feel like they move to the music rather than "
                    "merely change on time.")
    aspect: str = Field(
        default="9:16",
        description="9:16 for phones (the default), 1:1, 4:5, or 16:9.")
    transition: str = Field(
        default="crossfade", description="crossfade | cut")
    sync_to_beat: bool = Field(
        default=True,
        description="Cut on the music. Shot lengths are rounded to whole "
                    "beats of the track, so the picture changes when the "
                    "music does. Leave it on whenever there is music; the "
                    "pacing you ask for is kept, only nudged onto the beat.")
    atmosphere: bool = Field(
        default=True,
        description="A vignette and slow drifting motes of warm light over "
                    "the whole film. It is what makes a sequence of "
                    "photographs read as one piece. Turn it off for something "
                    "plain or corporate.")
    filename: str = Field(
        default="teaser.mp4", description="What to call the finished file.")


#: A storyboard longer than this is not a teaser, and the limit is stated in
#: the refusal so the model can shorten it rather than guess.
MAX_SHOTS = video.MAX_SHOTS


class VideoTool(Tool):
    name = "make_video"
    description = (
        "Render a short video from the photos, clips and audio the user has "
        "attached to this conversation — a teaser, a montage, a recap. You "
        "write the storyboard: which upload each shot shows, how long it is "
        "on screen, what it says, which way the camera drifts, and what plays "
        "underneath. Compass renders it and gives you a link to show.\n\n"
        "Only files attached to this conversation can be used, by the ids "
        "listed with the attachments. It cannot generate imagery that was "
        "never uploaded, and it cannot read anything else on the machine.\n\n"
        "Make the storyboard yourself rather than asking the user to specify "
        "every shot: look at what they attached, decide an order and a "
        "rhythm, caption sparingly, and say afterwards what you chose so they "
        "can ask for changes.\n\n"
        "How a good one is cut:\n"
        "- Open on a title card, close on a short end card.\n"
        "- Two to three seconds a shot for a held moment; under a second "
        "when you want a burst of them in a row. With music, lengths are "
        "rounded to the beat for you — ask for the rhythm you want and it "
        "lands on the beat.\n"
        "- Caption a few shots, not all of them. A caption every shot reads "
        "as subtitling; three or four across a minute reads as a film.\n"
        "- If the people in it do not speak English, write `caption` in their "
        "language and put the English in `subcaption`. Both are set properly, "
        "including Indic scripts, and the pair is the house style of this "
        "kind of teaser.\n"
        "- Group photographs of people into collages of two or three with "
        "`with_media`; a wall of faces carries more than each face alone.\n"
        "- Vary the motion between shots, and let the atmosphere stay on.\n"
        "- A community film that has sponsors opens on the title card and "
        "then a sponsor card: pass their logo ids as `sponsor_logos`.\n\n"
        "Using part of a track. When somebody says \"the song from 1:45 to "
        "2:45\", pass `music_start` and `music_end` — the renderer seeks into "
        "the file, and reads the tempo of that section rather than the whole "
        "song. Never tell them to trim an MP3 first; that is the tool's job. "
        "Plan a storyboard about as long as the section they asked for.\n\n"
        "Rhythm. A teaser is not one pace throughout. Hold the title and the "
        "opening shots for two to three seconds each, then cut a run of six "
        "or eight shots at around half a second for a burst, then hold again "
        "on the moment that deserves it. Sameness is what makes a montage "
        "feel like a slideshow, and with music the beat does the rest."
    )
    input_model = MakeVideoInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return False

    def check_tool_permissions(
        self, inp: BaseModel, ctx: ToolUseContext
    ) -> PermissionDecision | None:
        """Allowed, and narrowly.

        Home's broker denies everything by design, which is right for a
        surface with no file access — but this tool's only reachable paths are
        the uploads of the conversation it is running in, and its only output
        goes back into that same directory. There is nothing here for a person
        to weigh up: refusing would not protect anything, and asking would
        interrupt a render they just requested.
        """
        return PermissionDecision(
            Behavior.ALLOW,
            "renders only this conversation's own uploads, into its own folder",
        )

    async def call(
        self, inp: MakeVideoInput, ctx: ToolUseContext
    ) -> AsyncIterator[ToolYield]:
        available = media.listing(ctx.session_id)
        if not available:
            yield ToolOutput(
                "Nothing has been attached to this conversation, so there is "
                "nothing to make a video from. Ask the user to attach the "
                "photos, clips or audio they want in it.",
                is_error=True)
            return

        if not inp.shots:
            yield ToolOutput(
                "A storyboard needs at least one shot. The uploads available "
                "are:\n" + _catalogue(available), is_error=True)
            return
        if len(inp.shots) > MAX_SHOTS:
            yield ToolOutput(
                f"{len(inp.shots)} shots is past the {MAX_SHOTS}-shot limit. "
                "Pick the strongest ones.", is_error=True)
            return

        shots: list[video.Shot] = []
        missing: list[str] = []
        for shot in inp.shots:
            path = media.resolve(ctx.session_id, shot.media)
            if path is None:
                missing.append(shot.media)
                continue
            if media_kind(path.name) == "sound":
                yield ToolOutput(
                    f"{shot.media} is a sound file, so it cannot be a shot. "
                    "Pass it as `music` instead.", is_error=True)
                return
            extra: list = []
            for ref in shot.with_media[:2]:
                found = media.resolve(ctx.session_id, ref)
                if found is None:
                    missing.append(ref)
                else:
                    extra.append(found)
            shots.append(video.Shot(
                source=path, with_=extra, seconds=shot.seconds,
                caption=shot.caption, subcaption=shot.subcaption,
                start=shot.start, motion=shot.motion, fill=shot.fill))
        if missing:
            yield ToolOutput(
                "No upload here is called " + ", ".join(repr(m) for m in missing)
                + ". The ones that exist are:\n" + _catalogue(available)
                + "\nUse the id exactly as written.", is_error=True)
            return

        music = media.resolve(ctx.session_id, inp.music) if inp.music else None
        if inp.music and music is None:
            yield ToolOutput(
                f"No upload here is called {inp.music!r}, so there is nothing "
                "to play underneath. The ones that exist are:\n"
                + _catalogue(available), is_error=True)
            return

        wall = None
        if inp.sponsor_logos:
            logos: list = []
            for ref in inp.sponsor_logos[:12]:
                found = media.resolve(ctx.session_id, ref)
                if found is None:
                    missing.append(ref)
                else:
                    logos.append(found)
            if missing:
                yield ToolOutput(
                    "No upload here is called " + ", ".join(repr(m) for m in missing)
                    + ". The ones that exist are:\n" + _catalogue(available),
                    is_error=True)
                return
            wall = video.LogoWall(
                logos=logos, heading=inp.sponsor_heading,
                subheading=inp.sponsor_subheading,
                card=video.Card(text=inp.title, subtitle=inp.subtitle,
                                kicker=inp.kicker) if inp.title.strip() else None)

        start_at = _seconds(inp.music_start)
        end_at = _seconds(inp.music_end)
        if end_at and end_at <= start_at:
            yield ToolOutput(
                f"The music ends ({inp.music_end}) at or before it starts "
                f"({inp.music_start}). Give a later end, or leave it empty to "
                "run to the end of the storyboard.", is_error=True)
            return
        run_for = (end_at - start_at) if end_at else 0.0

        spec = video.Spec(
            shots=shots,
            sponsors=wall,
            title=video.Card(text=inp.title, subtitle=inp.subtitle,
                             kicker=inp.kicker)
            if inp.title.strip() else None,
            end_card=video.Card(text=inp.end_text, seconds=1.8)
            if inp.end_text.strip() else None,
            music=music,
            aspect=inp.aspect if inp.aspect in video.TARGETS else "9:16",
            transition="cut" if inp.transition == "cut" else "crossfade",
            sync_to_beat=inp.sync_to_beat,
            atmosphere=inp.atmosphere,
            beat_pulse=inp.beat_pulse,
            music_start=start_at,
            music_seconds=run_for,
        )

        out = media.output_path(ctx.session_id, inp.filename)
        # Progress is for the person watching, not the model: a render is a
        # minute of silence otherwise, and silence is what makes a turn look
        # dead. The renderer runs as a task and reports through a queue, so
        # each shot is announced as it is encoded rather than all of them
        # afterwards, which is what collecting into a list would have done.
        updates: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(
            video.render_async(spec, out, on_progress=updates.put_nowait))
        yield Progress(f"Rendering {len(shots)} shots…\n")
        while not task.done():
            try:
                line = await asyncio.wait_for(updates.get(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            yield Progress(f"  {line}\n")
        while not updates.empty():
            yield Progress(f"  {updates.get_nowait()}\n")

        try:
            result = await task
        except video.VideoUnavailable as err:
            yield ToolOutput(str(err), is_error=True)
            return
        except video.RenderError as err:
            yield ToolOutput(f"The render failed: {err}", is_error=True)
            return
        except Exception as err:  # noqa: BLE001 — a tool never takes the turn down
            logger.exception("make_video failed")
            yield ToolOutput(f"The render failed: {err}", is_error=True)
            return

        # Said out loud, because the alternative is handing someone a film
        # whose captions are quietly unreadable. Only when it actually bites:
        # a Latin caption sets correctly either way.
        complex_script = any(
            textkit.script_of(line) not in {"latin"}
            for shot in inp.shots for line in (shot.caption, shot.subcaption) if line
        ) or any(textkit.script_of(x) != "latin"
                 for x in (inp.title, inp.subtitle, inp.end_text, inp.kicker) if x)
        warning = ""
        if complex_script and not textkit.shaping_available():
            warning = ("\n\nSay this to the user: the captions are in a script "
                       "that needs text shaping, and the shaping libraries are "
                       "not installed on this server, so letters that should "
                       "join may be drawn separately. Installing `uharfbuzz` "
                       "and `freetype-py` fixes it.")

        # Dissolves shorten a film, and a storyboard written to fill sixty
        # seconds does not. Said in numbers the model can act on rather than
        # left for the person to notice.
        if result.short_by >= 1.0:
            gap = result.short_by
            more = max(1, round(gap / 2.2))
            warning += (f"\n\nThe film runs {result.seconds:g}s but you asked "
                        f"for {gap + result.seconds:g}s of music, so it is "
                        f"{gap:g}s short — each dissolve costs half a second. "
                        f"Offer to add about {more} more shot(s) and re-render "
                        "if they want it to fill the section.")

        # The finished film is written through to storage like an upload: it is
        # one more file belonging to the thread, it is the largest one here
        # (30MB), and it is the only copy until this runs.
        await media.upload(
            ctx.session_id,
            [media.MediaFile(id=result.path.name, name=result.path.name,
                             kind="video", path=result.path, bytes=result.bytes)],
            owner=ctx.owner,
        )
        url = media.url_for(ctx.session_id, result.path.name)
        yield ToolOutput(
            f"Rendered {result.path.name} — {result.seconds:g}s, "
            f"{result.width}x{result.height}, {result.bytes / 1_048_576:.1f} MB, "
            f"{len(shots)} shots"
            + (" with music" if music else " (no music)") + ".\n\n"
            "Show it by putting this link on a line of its own, exactly as "
            f"written:\n\n[{result.path.name}]({url})\n\n"
            "Then say in a sentence or two how you cut it — the order, the "
            "pacing, any captions — and offer to change it." + warning
        )


def _seconds(value: str) -> float:
    """Seconds from "1:45", "1:45.5", "105" or "" — the ways a person says a
    position in a song. Anything unreadable is treated as the start rather
    than refused: a mistyped timestamp should not lose the render."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        logger.warning("make_video: could not read the time %r", value)
        return 0.0
    total = 0.0
    for part in parts:  # h:m:s, m:s or s
        total = total * 60 + part
    return max(0.0, total)


def _catalogue(files: list[media.MediaFile]) -> str:
    return "\n".join(f"  {f.describe()}" for f in files)
