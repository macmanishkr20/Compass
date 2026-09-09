"""Estimate — should this be built with AI, and what will it cost?

A brief goes in: what the thing is, which features it has, which of them want a
model, how many people will use it and how hard the constraints are. An
estimate comes out: a feasibility score, an itemised first-year cost, an
AI-versus-standard comparison, a token projection and an ROI model — with the
verdict and the confidence in it stated rather than implied.

The founding rule, and the reason the figures are worth showing anyone: **a
model only ever resolves a label.** The classifier turns free text into one of
the catalog's task types; the architect picks one of five delivery platforms.
Both write their answer onto the brief, and every number after that is computed
by `engine.py` from versioned rate cards. Ask twice, get the same answer. Ask
with no model configured and get the same answer again, reached by keyword
rules. That is what makes an estimate defensible in a budget review, and it is
why the arithmetic is kept structurally out of reach of anything that talks.

The module is self-contained: it imports `compass.common` for settings, auth,
ownership and the model client, and nothing else from Compass — no other module
imports it, and it imports no other module. Switching it off with
`COMPASS_ESTIMATE=0` removes it completely rather than hiding it.
"""
