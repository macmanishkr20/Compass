"""Design — kept as the import path the rest of Compass already uses.

The parts now live in `compass.design`: the project store, the design
systems, the clarifying questions, and the template catalogue and briefs
under `skills`. This re-exports every name that has ever been imported
from here, so where a thing lives and where it is fetched from can change
independently.
"""

from __future__ import annotations

from compass.design import skills as _skills
from compass.design.skills.catalogue import TEMPLATES
from compass.design.store import (
    BLANK_PAGE,
    MAX_VERSIONS,
    DesignProject,
    DesignStore,
    get_design_store,
)
from compass.design.systems import (
    BUILTIN_SYSTEMS,
    EXTRACT_PROMPT,
    DesignSystem,
    DesignSystemStore,
    get_system_store,
    parse_extract,
    system_prompt_block,
)
from compass.design.clarify import (
    CLARIFY_PROMPT,
    FOLLOWUP_FALLBACK,
    FOLLOWUP_PROMPT,
    clarify_answers_block,
    normalize_clarify,
)

TEMPLATE_PROMPTS = _skills.TEMPLATE_PROMPTS
DESIGN_SYSTEM_PROMPT = _skills.DESIGN_SYSTEM_PROMPT

__all__ = [
    "BLANK_PAGE",
    "BUILTIN_SYSTEMS",
    "CLARIFY_PROMPT",
    "DESIGN_SYSTEM_PROMPT",
    "EXTRACT_PROMPT",
    "FOLLOWUP_FALLBACK",
    "FOLLOWUP_PROMPT",
    "MAX_VERSIONS",
    "TEMPLATES",
    "TEMPLATE_PROMPTS",
    "DesignProject",
    "DesignStore",
    "DesignSystem",
    "DesignSystemStore",
    "clarify_answers_block",
    "get_design_store",
    "get_system_store",
    "normalize_clarify",
    "parse_extract",
    "system_prompt_block",
]
