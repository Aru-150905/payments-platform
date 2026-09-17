#!/usr/bin/env bash
# End-to-end smoke test: create two accounts, fund one, pay, capture.
set -euo pipefail

# A unique key per run. The script creates fresh accounts each time, so
# reusing a fixed key would return the PREVIOUS run's payment against
# accounts that no longer relate to it — idempotency working correctly, but
# making the demo prove nothing on any run after the first.
KEY="demo-$(date +%s)-$RANDOM"
API=${API:-http://localhost:8000}
# Every route but /health/* and /metrics requires this — see ADR 0007
# Decision 4. Override with a real value via API_KEY= if api_key was changed
# from its default in .env.
API_KEY=${API_KEY:-dev-local-key}
AUTH=(-H "X-API-Key: $API_KEY")

payer=$(curl -sX POST $API/accounts "${AUTH[@]}" -H 'content-type: application/json' \
  -d '{"owner_id":"user_1","name":"wallet","currency":"INR","allow_negative":true}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
payee=$(curl -sX POST $API/accounts "${AUTH[@]}" -H 'content-type: application/json' \
  -d '{"owner_id":"merchant_1","name":"wallet","currency":"INR"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "payer=$payer payee=$payee"

pay=$(curl -sX POST $API/payments "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: $KEY" \
  -d "{\"payer_account_id\":\"$payer\",\"payee_account_id\":\"$payee\",\"amount_minor\":250000}")
echo "authorized: $pay"
id=$(echo "$pay" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "--- retry with the SAME idempotency key (must not create a second payment) ---"
curl -sX POST $API/payments "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: $KEY" \
  -d "{\"payer_account_id\":\"$payer\",\"payee_account_id\":\"$payee\",\"amount_minor\":250000}"; echo

echo "--- capture ---"
curl -sX POST $API/payments/$id/capture "${AUTH[@]}"; echo
echo "--- capture again (must be 409) ---"
curl -sX POST $API/payments/$id/capture "${AUTH[@]}"; echo

echo "--- balances ---"
curl -s $API/accounts/$payer/balance "${AUTH[@]}"; echo
curl -s $API/accounts/$payee/balance "${AUTH[@]}"; echo

echo "--- missing API key (must be 401) ---"
curl -s -o /dev/null -w '%{http_code}\n' $API/accounts/$payer/balance
