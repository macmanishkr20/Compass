#!/usr/bin/env bash
# Run the Compass backend on this machine alone, with nothing reaching Azure.
#
#     scripts/run_local.sh            # backend on :8000
#     COMPASS_PORT=8010 scripts/run_local.sh
#
# A shortcut for scripts/run_local.py, which does the work and says what it
# hides; on Windows run that one directly.
set -euo pipefail

cd "$(dirname "$0")/.."

python="python3"
[ -x .venv/bin/python ] && python=".venv/bin/python"

exec "$python" scripts/run_local.py
