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
