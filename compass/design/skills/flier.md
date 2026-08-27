Design a flier — a poster, pinned to a wall and read by someone standing a few steps away. It is not a page, and it is certainly not a dashboard.

One sheet, portrait, A4: 794px wide by 1123 tall, centred on a plain ground with a soft shadow and a margin of about 56px inside it. Everything is ON that one sheet. Use min-height so nothing can ever collide, but treat 1123 as the size: if the content will not fit, write less of it or set it smaller — never let the sheet run on. A flier 1800px tall is a web page someone printed by mistake, and it is the most common way this goes wrong.

Keep the whole thing under about 180 words. A poster is read standing up.

The sheet is a column in normal flow: header, body, foot, one after another, with box-sizing: border-box and the foot last. Nothing is absolutely positioned over anything else, nothing overlaps, and nothing is clipped.

Three tiers, and someone three steps back takes them in this order:
- WHAT IT IS. The event's name, the largest thing on the sheet by a long way — four times the body text at least, 56px and upwards — sitting in the top third, two or three words to a line, never clipped. If it does not fit the width, set it smaller. The occasion's motif sits beside or behind it.
- WHEN AND WHERE. The date, the time and the place, readable at a glance at 20 to 28px, as a few short lines. Not a table, not a row of cards, not a definition list under an 11px letterspaced label.
- EVERYTHING ELSE. The programme, what to bring, who to call, how to give. Small, quiet, and last.
Group with space, and at most a hairline rule. Not with a grid of bordered, rounded cards: that is an interface, and it flattens the hierarchy until every part of the sheet looks equally important — the one thing a flier must never do. Three panels on the whole sheet is plenty, and none at all is the usual answer.

Type: a display face for the name, a humanist sans for everything else, nothing below 11px because this is paper. The monospace belongs on a flier only in a UPI id, a reference code or a URL — a date, a time or a sentence set in mono reads as a receipt.

Colour comes from the occasion, not from the house palette: a Ganpati flier is marigold, saffron, vermilion and gold on cream; a school fair, a funeral notice and a warehouse sale are each different again. Choose three or four and commit to them. A festival poster may be more than half colour, and that is correct — the restraint that suits a dashboard would make this look like a form.
- What to do next: the call to action, and the way to reach someone, at the foot of the sheet and above the bottom margin.
- If it is the kind of flier someone tears a number off — an event, a class, a thing for sale, a service — finish it with a row of tear-off strips across the very bottom: six to ten equal strips inside the foot row, divided by dashed rules, each carrying the same short line (the name of the thing and the number to call) set vertically with writing-mode. They are part of the foot, so they sit inside the sheet like everything else. A notice with nothing to take away does not get them.
Print colours — flat, saturated, and true when they come off a printer. Any ornament is drawn as inline SVG. No scrollbars, no hover-only detail, and nothing that only makes sense on a screen.