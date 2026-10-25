# Estimator

The Compass **Estimate** module, standing on its own.

Lifted out rather than rewritten: the intake, the engine, the catalog and
the export are the same code, so an estimate produced here is the estimate
Compass produced, figure for figure. `scripts/check_estimate.py` is the
same suite and still passes — 255 checks, including the one that asserts
the model cannot reach the arithmetic.

## What came across

```
estimator/estimate    the module: intake, engine, catalog, architect, export
estimator/common      only the parts it reached for — settings, auth,
                      ownership, the model gateway, the catalog store
estimator/api         the ASGI app, which mounts exactly one router
frontend/             the estimate screen, its report, and a shell
scripts/              the check suite
```

## What did not

Six sibling modules, and with them the MCP manager, the routine scheduler,
the pipeline trigger loop and the mission scheduler — each was started by
Compass's lifespan for a module that does not exist here.

The Pipelines link went too. Compass let a pipeline name the estimate it
implemented; nothing depends on this application that way, so the check
that asserted the dependency ran one direction is gone rather than left
passing vacuously.

## Running it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in the model endpoint
PYTHONPATH=. uvicorn estimator.api.server:app --port 8000
```

```bash
cd frontend && npm install && npm start    # proxies /v1 to :8000
```

With no model endpoint configured the app starts and every screen works;
the three generative steps answer 501 and say what is missing. With
`COMPASS_STORAGE_BACKEND=local` it needs no Azure at all.

```bash
COMPASS_AUTH_ENABLED=0 PYTHONPATH=. python3 scripts/check_estimate.py
```

## Known rough edges, named rather than discovered

- **`COMPASS_` is still the env prefix.** The settings code is Compass's,
  unchanged, and renaming the prefix means rewriting `config.py`. Rename
  it the day that file is trimmed, not before.
- **`estimator/common/config.py` is 1,186 lines** and still declares
  settings for pipelines, missions, sandboxes and business functions.
  Harmless — nothing reads them — but it is the obvious first trim.
- **Default resource names still say `compass`** (`cosmos_database`,
  `blob_container`, `queue_name`). They are env-overridable; changing the
  defaults would point a fresh deployment at resources that do not exist.
- **`estimate.css` is 4 kB over its budget.** It was over Compass's too.

## Why a copy and not a shared library

The two will drift, and that is the point of splitting them: this answers
to whoever owns estimating, on its own release cadence, and a change here
cannot break a gift chain in another product. If they must not drift, do
not split them.
