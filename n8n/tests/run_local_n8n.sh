#!/bin/sh
# Imports the template and an offline test variant into a throwaway local n8n and executes the variant
# against a local mock of the Apify endpoint. Needs: n8n installed at $N8N_DIR (npm i n8n --ignore-scripts).
set -e
N8N_DIR=${N8N_DIR:-/tmp/n8n}
export N8N_USER_FOLDER=${N8N_USER_FOLDER:-$(mktemp -d)} N8N_DIAGNOSTICS_ENABLED=false N8N_RUNNERS_ENABLED=false
HERE=$(cd "$(dirname "$0")" && pwd); T=$(mktemp -d); PORT=${PORT:-18932}
N="node $N8N_DIR/node_modules/.bin/n8n"
$N import:workflow --input="$HERE/../workday-jobs-to-google-sheets.n8n.json" 2>&1 | tail -1
python3 "$HERE/make_test_variant.py" "$T/test.json" "$PORT"
$N import:workflow --input="$T/test.json" 2>&1 | tail -1
python3 "$HERE/mock_apify.py" "$PORT" "$T/requests.jsonl" & MOCK=$!
trap 'kill $MOCK' EXIT
sleep 1
$N execute --id TestWorkdayJobs01 --rawOutput > "$T/out.json" 2>"$T/err.log" || { cat "$T/err.log"; exit 1; }
python3 "$HERE/check_run.py" "$T/out.json" "$T/requests.jsonl"
