"""The questions asked before a design is written.

A one-line brief makes a generic design. These decide whether there is
enough to work from, ask for what is missing as a small form, and turn the
answers back into a paragraph the model reads.
"""

from __future__ import annotations

CLARIFY_PROMPT = (
    "You are Compass Design deciding whether a request is specific enough to "
    "design from.\n\n"
    "If it is — there is a subject and enough intent to make something real — "
    'reply exactly: {"ready": true}\n\n'
    "If it is not — the subject is missing, or the sentence trails off — reply "
    "with a short form that would settle it, as JSON:\n"
    '{"ready": false, "title": "What should I model?", '
    '"waiting": "Waiting on object selection", '
    '"note": "Waiting on the form — mainly which object you want.", '
    '"subtitle": "Your message cut off before the object — tell me what to '
    'build and I\'ll start.", "fields": [\n'
    '  {"id":"object","label":"The object","hint":"Be as specific as you like '
    '— brand-free, but details help","type":"textarea","max":400,'
    '"placeholder":"e.g. a 1960s desk fan, a moka pot, a hand plane"},\n'
    '  {"id":"detail","label":"How detailed?","hint":"Detail costs nothing to '
    'view, but simpler reads cleaner","type":"segmented",'
    '"options":["Clean and simple","Balanced","Highly detailed"],'
    '"value":"Balanced"},\n'
    '  {"id":"style","label":"Modeling style","type":"radio",'
    '"options":["Photoreal proportions","Stylized / toy-like",'
    '"Technical / product-shot neutral","Low-poly faceted"]},\n'
    '  {"id":"use","label":"What\'s it for?","hint":"Shapes how I trade off '
    'polish vs. export cleanliness","type":"checkbox",'
    '"options":["Just to look at / spin around","Import into Blender or a 3D '
    'tool","Game or AR asset"]}\n]}\n\n'
    "Rules: four or five fields, never more. The first is always the missing "
    "subject, and the last asks for anything that must be present — the "
    "specific parts or sections the person cares about. Write the questions "
    "for THIS request: a deck asks about audience and length, a résumé about "
    "the role, a landing page about the product.\n"
    '"waiting" is a four-word status for the project while it waits '
    '("Waiting on object selection"). "note" is one sentence saying what is '
    "being waited on. Reply with JSON and nothing else."
)


# Asked again after a first round: what else would sharpen this?
FOLLOWUP_PROMPT = (
    "You already asked about this design and got the answers below. The person "
    "then pressed \"Ask me follow-up questions\" — they asked for another round, "
    "so there is always one to give. Return three or four NEW questions in the "
    "same JSON shape ({\"ready\": false, \"title\", \"waiting\", \"note\", "
    "\"subtitle\", \"fields\"}), with the same field types. Never repeat a "
    "question already answered, and never re-ask the subject. Go one level "
    "finer than the first round: materials and finish, colour and palette, "
    "lighting or mood, scale and proportion, setting or background, motion, "
    "labelling, what to leave out. Never reply {\"ready\": true} — they asked to "
    "be asked. JSON only."
)


# What the second round falls back to when the model has nothing to add: the
# questions that sharpen almost any design. Asked-for questions get asked.
FOLLOWUP_FALLBACK = {
    "ready": False,
    "title": "A few more details",
    "waiting": "Waiting on the details",
    "note": "A second round — finish, colour, mood, and what to leave out.",
    "subtitle": "Nothing here is required; anything you skip, I'll choose.",
    "fields": [
        {
            "id": "materials",
            "label": "Materials and finish",
            "hint": "Surfaces, textures, how new or worn it should read",
            "type": "textarea",
            "max": 300,
            "placeholder": "e.g. satin painted steel, light patina, chrome trim",
        },
        {
            "id": "palette",
            "label": "Colour direction",
            "type": "radio",
            "options": [
                "Muted and natural",
                "Bold and saturated",
                "Mostly monochrome",
                "Follow the design system",
            ],
        },
        {
            "id": "mood",
            "label": "Mood",
            "type": "radio",
            "options": [
                "Warm and inviting",
                "Clean and technical",
                "Playful",
                "Serious and premium",
            ],
        },
        {
            "id": "avoid",
            "label": "Anything to leave out",
            "hint": "Details, elements, or clichés you would rather not see",
            "type": "textarea",
            "max": 300,
            "placeholder": "e.g. no gradients, no stock-photo people, no drop shadows",
        },
    ],
}


# The form only knows five field types. A model asked for JSON will now and
# then reach for a sixth ("select", "multiselect", "short_text") or hand back
# options as objects rather than strings, so every form is put through this
# before it reaches the canvas.
_FIELD_TYPES = {
    "textarea": "textarea",
    "text": "text",
    "string": "text",
    "input": "text",
    "short_text": "text",
    "segmented": "segmented",
    "toggle": "segmented",
    "radio": "radio",
    "select": "radio",
    "dropdown": "radio",
    "choice": "radio",
    "single_select": "radio",
    "checkbox": "checkbox",
    "multiselect": "checkbox",
    "multi_select": "checkbox",
    "checkboxes": "checkbox",
}


def normalize_clarify(form: dict) -> dict:
    """Coerce a model's form into the shapes the canvas can render."""
    fields = []
    for raw in form.get("fields") or []:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("title") or "").strip()
        if not label:
            continue
        options = []
        for opt in raw.get("options") or []:
            if isinstance(opt, dict):
                text = opt.get("label") or opt.get("title") or opt.get("value")
            else:
                text = opt
            text = str(text or "").strip()
            if text:
                options.append(text)

        kind = _FIELD_TYPES.get(str(raw.get("type") or "").strip().lower(), "")
        if not kind:
            kind = "radio" if options else "textarea"
        if kind in {"radio", "checkbox", "segmented"} and not options:
            kind = "text"
        # Three short options read better as a segmented control; more than
        # that, or anything wordy, wants the stacked list.
        if kind == "segmented" and (len(options) > 3 or max(map(len, options)) > 22):
            kind = "radio"

        field = {
            "id": str(raw.get("id") or label.lower().replace(" ", "_"))[:48],
            "label": label,
            "type": kind,
        }
        for key in ("hint", "placeholder"):
            if raw.get(key):
                field[key] = str(raw[key])
        if options:
            field["options"] = options
        if raw.get("max"):
            try:
                field["max"] = int(raw["max"])
            except (TypeError, ValueError):
                pass
        value = raw.get("value")
        if isinstance(value, str) and (not options or value in options):
            field["value"] = value
        fields.append(field)

    form = dict(form)
    form["fields"] = fields
    return form


def clarify_answers_block(answers: dict) -> str:
    """Fold a filled-in form back into the brief."""
    lines = []
    for label, value in answers.items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if str(value).strip():
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)
