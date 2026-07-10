# Compass UI

Angular 21 front-end for the Compass agent — an enterprise console with a
depth-aware 3D visual language, streaming over the FastAPI backend's SSE.

## Run

```bash
# 1. backend (from repo root)
COMPASS_MOCK_MODEL=1 .venv/bin/uvicorn compass.api.server:app --port 8000

# 2. this app (proxies /v1 and /healthz to :8000 — see proxy.conf.json)
cd frontend
npm install
npm start            # http://localhost:4200
```

For real Azure OpenAI, drop `COMPASS_MOCK_MODEL=1` and fill the backend `.env`.
To point at a different backend host, edit `proxy.conf.json`.

To demo the **permission Allow/Deny cards** without credentials, start the
backend with `COMPASS_MOCK_SCENARIO=permission` — the mock model issues a
mutating command, the UI shows the approval card, and the closing reply
adapts to your verdict.

## Architecture

Zoneless, signals-only, standalone components, `OnPush` everywhere.

| Piece | File | Role |
|---|---|---|
| App shell + SSE reducer | `src/app/app.ts` | one signal `timeline`, reduced from the event stream |
| Streaming client | `src/app/compass-api.service.ts` | `fetch`-based SSE reader (EventSource can't POST) |
| Theme | `src/app/theme.service.ts` | light/dark, persisted, `data-theme` on `<html>` |
| Design system | `src/styles.css` | tokens for both themes, glass + `.tilt-3d` primitives, parallax depth field |

## The 3D, deliberately

Every effect is real depth, not decoration, and every one degrades under
`prefers-reduced-motion`:

- **Gyroscopic compass rose** (`compass-rose/`) — three rings projected as
  tilted ellipses on a 2D canvas with a north-seeking needle; accelerates
  while a turn is streaming. Honest perspective math, no WebGL dependency.
- **Pointer-tracked card tilt** (`tilt.directive.ts`) — tool, permission, and
  composer cards rotate toward the cursor in a shared perspective space with a
  light sheen that follows the pointer. rAF-batched CSS variables, so it never
  triggers Angular change detection.
- **In-card parallax** — headers and action rows sit on `translateZ` planes, so
  they float above the card surface as it tilts.
- **Layered depth field** — drifting radial glows + a dotted grid behind glass
  panels give the whole app a Z-axis.
- **Card-in / rise animations** on bubbles, cards, and the hero.

## Accessibility

Dual-theme with real contrast in both, visible focus rings, `aria-live` on the
log, keyboard-first composer (Enter to send, Shift+Enter for newline), and a
full reduced-motion path that flattens every transform and animation.
