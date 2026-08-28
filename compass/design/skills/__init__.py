"""The design skills — one file per template.

A template's brief is prose, and prose belongs in a prose file. Each of these
is loaded verbatim: what a skill file contains is what the model is told, so
editing one of them changes what Compass builds, and nothing else has to be
touched.

`house.md` is not a template. It is the house style every design is held to,
whatever is being built.

Read exactly, with no stripping. Trailing whitespace is part of the prompt —
one of these ends in a blank line and the others end with no newline at all,
and a loader that tidied that up would be quietly editing the brief.
"""

from pathlib import Path

_HERE = Path(__file__).parent
_HOUSE = "house"


def _read(name: str) -> str:
    return (_HERE / f"{name}.md").read_text(encoding="utf-8")


def skill_names() -> list[str]:
    """Every template that has a brief of its own."""
    return sorted(p.stem for p in _HERE.glob("*.md") if p.stem != _HOUSE)


#: What each template asks for, by template id.
TEMPLATE_PROMPTS: dict[str, str] = {name: _read(name) for name in skill_names()}

#: The house style, which applies to all of them.
DESIGN_SYSTEM_PROMPT: str = _read(_HOUSE)

#: The sentence the tweak-sheet instruction opens with. Everything from here
#: to the end of the house style is that instruction.
_TWEAK_MARKER = "End the document with a tweak sheet"

#: Templates that get the house style without the tweak sheet.
#:
#: A tweak sheet is a panel of live controls — colour swatches, sliders, a
#: toggle — appended to the end of a design so someone can retune it. That is
#: worth having on something meant to be used on screen. On a document it is
#: an eighth page of sliders after the conclusion, and a document is a thing
#: people print: in print the controls cannot be moved, and what remains is a
#: page of dead widgets where the last page of prose should be.
#:
#: Only `document` for now, because that is what was asked for. The same
#: argument applies to the résumé, the flier and the slides — all of them are
#: printed pages too — so this is the set to extend if those come up.
NO_TWEAK_SHEET = frozenset({"document"})


def system_prompt_for(template: str | None) -> str:
    """The house style as this template should hear it.

    Falls back to the whole thing if the marker is ever edited out of
    house.md, on the principle that too much brief is a worse failure than a
    stale constant: a design with an unwanted panel is fixable, a design built
    without the house style is not what anyone asked for.
    """
    if (template or "") not in NO_TWEAK_SHEET:
        return DESIGN_SYSTEM_PROMPT
    head, marker, _ = DESIGN_SYSTEM_PROMPT.partition(_TWEAK_MARKER)
    return head.rstrip() if marker else DESIGN_SYSTEM_PROMPT
