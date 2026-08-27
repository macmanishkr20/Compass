"""What Compass Design offers on its landing screen.

One entry per template: the id a brief is looked up by, the name and hint
shown on the card, and the opening words the composer fills in for you.
The brief itself is the matching file beside this one.
"""

from __future__ import annotations

# The templates Claude Design offers on its landing screen.
TEMPLATES = [
    {"id": "blank", "name": "Blank", "hint": "Start from nothing", "stem": ""},
    {
        "id": "mobile",
        "name": "Mobile app design",
        "hint": "Screens for a phone app",
        "stem": "Design a mobile app for ",
    },
    {"id": "slides", "name": "Slides", "hint": "A deck to present", "stem": "Make a deck about "},
    {
        "id": "document",
        "name": "Document",
        "hint": "A formatted written page",
        "stem": "Write and lay out a document on ",
    },
    {
        "id": "wireframe",
        "name": "Wireframe",
        "hint": "Low-fidelity structure",
        "stem": "Wireframe the flow for ",
    },
    {
        "id": "animation",
        "name": "Animation",
        "hint": "Something that moves",
        "stem": "Animate ",
    },
    {
        "id": "mockups",
        "name": "UI mockups",
        "hint": "High-fidelity screens",
        "stem": "Mock up the screens for ",
    },
    {"id": "resume", "name": "Résumé", "hint": "A one-page CV", "stem": "A résumé for "},
    {
        "id": "object3d",
        "name": "3D object",
        "hint": "A rendered object",
        "stem": "Model a 3D object: ",
    },
    {
        "id": "research",
        "name": "Research",
        "hint": "Findings, written up",
        "stem": "Write up research on ",
    },
    {
        "id": "email",
        "name": "HTML email",
        "hint": "An email that renders",
        "stem": "An HTML email announcing ",
    },
    {
        "id": "colortype",
        "name": "Color + type pairing",
        "hint": "A palette and typefaces",
        "stem": "A colour and type pairing for ",
    },
    {
        "id": "diagram",
        "name": "Diagram",
        "hint": "Boxes, arrows, a system",
        "stem": "Draw a diagram of ",
    },
    {
        "id": "flier",
        "name": "Flier",
        "hint": "A poster or handout",
        "stem": "Design a flier for ",
    }
]

# Per-template guidance appended to the generation prompt.
# The per-template briefs live one to a file in compass/design/skills, so a
# brief can be read and edited as the prose it is. Re-exported here because
# this is where the rest of the code has always found them.
