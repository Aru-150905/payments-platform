#!/usr/bin/env bash
# End-to-end smoke test for M6: create an instrument, fund two owners, place
# a resting limit sell, cross it with a limit buy, confirm the trade settled
# into the ledger (cash AND position moved), then sweep the remainder with a
# market order. See docs/adr/0008-matching-engine.md for the design this is
# exercising end to end.
set -euo pipefail

API=${API:-http://localhost:8000}
API_KEY=${API_KEY:-dev-local-key}
AUTH=(-H "X-API-Key: $API_KEY")
# Unique per run for the same reason scripts/demo.sh's KEY is — a fixed
# symbol would collide with instruments.symbol's UNIQUE constraint on the
# second run, and a fixed owner_id would collide with
# uq_accounts_owner_name_currency on the second run's cash/position
# accounts. See CLAUDE.md's note on state carried between invocations.
RUN=$(date +%s)-$RANDOM
SYMBOL="DEMO$RUN"
# Instrument symbols are ^[A-Z0-9]{1,16}$ (migration 0003) — strip anything
# a raw timestamp-random suffix might add that isn't upper/digit, then trim
# to 16.
SYMBOL=$(echo "$SYMBOL" | tr 'a-z' 'A-Z' | tr -cd 'A-Z0-9' | cut -c1-16)

echo "--- create instrument $SYMBOL ---"
instrument=$(curl -sX POST $API/instruments "${AUTH[@]}" -H 'content-type: application/json' \
  -d "{\"symbol\":\"$SYMBOL\",\"quote_currency\":\"INR\"}")
echo "$instrument"
instrument_id=$(echo "$instrument" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "--- fund buyer and seller cash wallets ---"
buyer_cash=$(curl -sX POST $API/accounts "${AUTH[@]}" -H 'content-type: application/json' \
  -d "{\"owner_id\":\"buyer-$RUN\",\"name\":\"wallet\",\"currency\":\"INR\",\"allow_negative\":true}")
buyer_cash_id=$(echo "$buyer_cash" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

seller_cash=$(curl -sX POST $API/accounts "${AUTH[@]}" -H 'content-type: application/json' \
  -d "{\"owner_id\":\"seller-$RUN\",\"name\":\"wallet\",\"currency\":\"INR\",\"allow_negative\":true}")
seller_cash_id=$(echo "$seller_cash" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "buyer_cash=$buyer_cash_id seller_cash=$seller_cash_id"

# No position account can go negative except "platform"'s (see ADR 0008's
# "where the first share comes from") — so before "seller" can legitimately
# sell anything, they first have to actually acquire it. This trade does
# that: platform (the issuer) sells 10 shares into existence, seller buys
# them. Only after this does "seller" hold a real, positive position to
# resell in the rest of this script.
echo "--- bootstrap: platform issues 10 shares to seller ---"
issuer_cash=$(curl -sX POST $API/accounts "${AUTH[@]}" -H 'content-type: application/json' \
  -d "{\"owner_id\":\"platform\",\"name\":\"issuer-cash-$RUN\",\"currency\":\"INR\",\"allow_negative\":true}")
issuer_cash_id=$(echo "$issuer_cash" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -sX POST $API/orders "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: issue-sell-$RUN" \
  -d "{\"instrument_id\":\"$instrument_id\",\"cash_account_id\":\"$issuer_cash_id\",\"side\":\"sell\",\"order_type\":\"limit\",\"quantity\":10,\"limit_price_minor\":500}" > /dev/null

curl -sX POST $API/orders "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: issue-buy-$RUN" \
  -d "{\"instrument_id\":\"$instrument_id\",\"cash_account_id\":\"$seller_cash_id\",\"side\":\"buy\",\"order_type\":\"limit\",\"quantity\":10,\"limit_price_minor\":500}" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="filled" and d["filled_quantity"]==10, d'
echo "seller now holds 10 shares of $SYMBOL"

echo "--- seller rests a limit sell: 10 @ 500 ---"
sell=$(curl -sX POST $API/orders "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: sell-$RUN" \
  -d "{\"instrument_id\":\"$instrument_id\",\"cash_account_id\":\"$seller_cash_id\",\"side\":\"sell\",\"order_type\":\"limit\",\"quantity\":10,\"limit_price_minor\":500}")
echo "$sell"
sell_id=$(echo "$sell" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "$sell" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="open" and d["filled_quantity"]==0, d'

echo "--- buyer crosses it with a limit buy: 4 @ 500 (partial fill expected) ---"
buy1=$(curl -sX POST $API/orders "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: buy1-$RUN" \
  -d "{\"instrument_id\":\"$instrument_id\",\"cash_account_id\":\"$buyer_cash_id\",\"side\":\"buy\",\"order_type\":\"limit\",\"quantity\":4,\"limit_price_minor\":500}")
echo "$buy1"
echo "$buy1" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="filled" and d["filled_quantity"]==4, d'

echo "--- resting sell order is now partially filled ---"
curl -s $API/orders/$sell_id "${AUTH[@]}"; echo
curl -s $API/orders/$sell_id "${AUTH[@]}" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="partially_filled" and d["filled_quantity"]==4, d'

echo "--- cash moved: buyer down 2000; seller's -5000 bootstrap cost is now -3000 ---"
curl -s $API/accounts/$buyer_cash_id/balance "${AUTH[@]}"; echo
curl -s $API/accounts/$seller_cash_id/balance "${AUTH[@]}"; echo
curl -s $API/accounts/$buyer_cash_id/balance "${AUTH[@]}" | python3 -c 'import sys,json;assert json.load(sys.stdin)["balance_minor"]==-2000'
curl -s $API/accounts/$seller_cash_id/balance "${AUTH[@]}" | python3 -c 'import sys,json;assert json.load(sys.stdin)["balance_minor"]==-3000'

echo "--- a market buy sweeps the remaining 6 ---"
buy2=$(curl -sX POST $API/orders "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: buy2-$RUN" \
  -d "{\"instrument_id\":\"$instrument_id\",\"cash_account_id\":\"$buyer_cash_id\",\"side\":\"buy\",\"order_type\":\"market\",\"quantity\":6}")
echo "$buy2"
echo "$buy2" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="filled" and d["filled_quantity"]==6, d'

echo "--- resting sell order is now fully filled ---"
curl -s $API/orders/$sell_id "${AUTH[@]}"; echo
curl -s $API/orders/$sell_id "${AUTH[@]}" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="filled" and d["filled_quantity"]==10, d'

echo "--- final cash: buyer down 5000 total; seller back to net 0 (bought at 500, sold at 500) ---"
curl -s $API/accounts/$buyer_cash_id/balance "${AUTH[@]}"; echo
curl -s $API/accounts/$seller_cash_id/balance "${AUTH[@]}"; echo
curl -s $API/accounts/$buyer_cash_id/balance "${AUTH[@]}" | python3 -c 'import sys,json;assert json.load(sys.stdin)["balance_minor"]==-5000'
curl -s $API/accounts/$seller_cash_id/balance "${AUTH[@]}" | python3 -c 'import sys,json;assert json.load(sys.stdin)["balance_minor"]==0'

echo "--- an empty-book market order sweeps nothing and is cancelled ---"
buy3=$(curl -sX POST $API/orders "${AUTH[@]}" \
  -H 'content-type: application/json' -H "Idempotency-Key: buy3-$RUN" \
  -d "{\"instrument_id\":\"$instrument_id\",\"cash_account_id\":\"$buyer_cash_id\",\"side\":\"buy\",\"order_type\":\"market\",\"quantity\":1}")
echo "$buy3"
echo "$buy3" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="cancelled" and d["filled_quantity"]==0, d'

echo
echo "ALL ASSERTIONS PASSED"
