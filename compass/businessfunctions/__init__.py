"""Business functions: parts of the firm, each with its own applications.

Finance and Talent — not practices that serve clients, but the functions the
firm runs on. Finance holds Form 26, a tax-credit reconciliation; Talent holds
the Leave Management System. Adding a function adds a folder; adding a feature
adds a handler module and a line in a manifest.

    manifest.py   what a function and a feature ARE, and every rule about
                  what a manifest may claim. Pure description, touches no disk.
    registry.py   finding them, checking them, resolving their handlers.
    features/     the handlers: the data behind each feature and the
                  judgement about which rules apply right now.
    catalog/      the functions themselves, as data.

The split that matters: the manifest owns the words and the shape, the handler
owns the data and the when. A feature is an application, not a prompt, so it
cannot be wholly declarative — but everything a person might want to reword,
re-scope or switch off is in the manifest, where somebody who is not a
developer can reach it.

Off by default behind `COMPASS_BUSINESS_FUNCTIONS`, the same escape hatch
Pipelines and Estimate have: off, no routes are mounted, nothing is read, and
Compass is what it was before this package existed.

Not here yet, deliberately: the store behind the handlers (both carry clearly
marked fixtures), the assistant that proposes an action and waits, and the
routes. Each needs this layer's answer to "what exists and what may it do",
and none of them should be asking it a second way.
"""

from __future__ import annotations
