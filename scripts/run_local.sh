#!/usr/bin/env bash
# Run the Compass backend on this machine alone, with nothing reaching Azure.
#
#     scripts/run_local.sh            # backend on :8000
#     COMPASS_PORT=8010 scripts/run_local.sh
#
# For when Azure is unavailable, or simply not wanted: records go to the local
# store under ./data, answers come from the scripted mock model, and P&L shows
# its sample month. Sign-in still comes from .env, so it works as it always
# does. Nothing in .env changes; every override here is for this process only,
# and a shell variable always wins over the file.
#
# The frontend needs nothing different: `cd frontend && npm exec -- ng serve
# --port 4200` proxies to :8000 as usual.
#
# What you will not have: records kept in Cosmos (a fresh local store starts
# empty), files kept in Blob, and real model answers.
set -euo pipefail

cd "$(dirname "$0")/.."

export COMPASS_STORAGE_BACKEND="${COMPASS_STORAGE_BACKEND:-local}"
export COMPASS_MOCK_MODEL="${COMPASS_MOCK_MODEL:-1}"
export COMPASS_PNL_SAMPLE="${COMPASS_PNL_SAMPLE:-1}"

# Empty, not unset: python-dotenv leaves a variable that already exists alone,
# so these hide whatever .env says and no client is ever pointed at Azure.
for name in \
    AZURE_COSMOS_ENDPOINT AZURE_COSMOS_KEY \
    AZURE_STORAGE_CONNECTION_STRING \
    AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY \
    AZURE_OPENAI_REALTIME_ENDPOINT \
    AZURE_OPENAI_TTS_ENDPOINT AZURE_OPENAI_TTS_API_KEY \
    AZURE_KEY_VAULT_URL AZURE_SERVICE_BUS_CONNECTION_STRING AZURE_REDIS_URL \
    APPLICATIONINSIGHTS_CONNECTION_STRING; do
    export "$name="
done

python="python3"
[ -x .venv/bin/python ] && python=".venv/bin/python"

echo "Compass, local only: storage=$COMPASS_STORAGE_BACKEND mock_model=$COMPASS_MOCK_MODEL" \
     "pnl_sample=$COMPASS_PNL_SAMPLE port=${COMPASS_PORT:-8000}"
exec "$python" -m uvicorn compass.api.server:app --host 127.0.0.1 --port "${COMPASS_PORT:-8000}"
