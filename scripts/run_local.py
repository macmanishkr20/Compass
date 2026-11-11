"""Run the Compass backend on this machine alone, with nothing reaching Azure.

    python scripts/run_local.py                 # backend on :8000
    COMPASS_PORT=8010 python scripts/run_local.py

On Windows, from the project folder, with the virtual environment's Python:

    .venv\\Scripts\\python scripts\\run_local.py
    $env:COMPASS_PORT = "8010"; .venv\\Scripts\\python scripts\\run_local.py

For when Azure is unavailable, or simply not wanted: records go to the local
store under ./data, answers come from the scripted mock model, and P&L shows
its sample month. Sign-in still comes from .env, so it works as it always
does. Nothing in .env changes; every override here is for this process only,
and a shell variable always wins over the file.

The frontend needs nothing different: `cd frontend && npm exec -- ng serve
--port 4200` proxies to :8000 as usual.

What you will not have: records kept in Cosmos (a fresh local store starts
empty), files kept in Blob, and real model answers.

This is Python rather than shell so it runs the same on Windows: there an
empty variable set in PowerShell or cmd is removed, not kept, and .env would
fill it back in. Set here, inside the server's own process, it holds.
"""

import os
import sys
from pathlib import Path

AZURE = (
    "AZURE_COSMOS_ENDPOINT", "AZURE_COSMOS_KEY",
    "AZURE_STORAGE_CONNECTION_STRING",
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_REALTIME_ENDPOINT",
    "AZURE_OPENAI_TTS_ENDPOINT", "AZURE_OPENAI_TTS_API_KEY",
    "AZURE_KEY_VAULT_URL", "AZURE_SERVICE_BUS_CONNECTION_STRING", "AZURE_REDIS_URL",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))

    os.environ.setdefault("COMPASS_STORAGE_BACKEND", "local")
    os.environ.setdefault("COMPASS_MOCK_MODEL", "1")
    os.environ.setdefault("COMPASS_PNL_SAMPLE", "1")
    # Empty, not unset: python-dotenv leaves a variable that already exists alone,
    # so these hide whatever .env says and no client is ever pointed at Azure.
    for name in AZURE:
        os.environ[name] = ""

    port = int(os.environ.get("COMPASS_PORT") or 8000)
    print(
        f"Compass, local only: storage={os.environ['COMPASS_STORAGE_BACKEND']}"
        f" mock_model={os.environ['COMPASS_MOCK_MODEL']}"
        f" pnl_sample={os.environ['COMPASS_PNL_SAMPLE']} port={port}",
        flush=True,
    )

    import uvicorn

    uvicorn.run("compass.api.server:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
