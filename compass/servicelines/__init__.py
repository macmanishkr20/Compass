"""Service lines: one agent practice per part of the firm, declared not coded.

Tax, Talent, Finance, Risk Consulting. Each is a folder under `catalog/` with
a manifest, a lead prompt and a set of skills. Adding one adds a folder.

Three files, and the split is deliberate:

  manifest.py   what a service line and a skill ARE, and every rule about
                what a manifest may claim. Pure description; touches no disk.
  registry.py   finding them, checking them and holding them in memory.
  catalog/      the service lines themselves, as data.

Nothing is imported from here unless `COMPASS_SERVICELINES` is on, the same
escape hatch Pipelines and Estimate have: off, no routes are mounted, nothing
is read, and Compass is byte-for-byte what it was before this package existed.

What is NOT here yet, and is on purpose: the engine that runs a skill, the
form schema loader, the engagement store and the routes. This layer answers
"what service lines exist and what may they do" and nothing else, because
every one of those later pieces needs this answer and none of them should be
asking the question a second way.
"""

from __future__ import annotations
