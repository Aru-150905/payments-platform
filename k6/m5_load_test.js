// k6 load test for M5's rate limiter, circuit breaker, and auth.
//
// Run with: k6 run k6/m5_load_test.js
// (k6 is a standalone Go binary, not a Python dependency — `brew install k6`
// or see https://k6.io/docs/get-started/installation/. Not containerized:
// it drives the same host-run API the Makefile's `make api` starts.)
//
// Env vars:
//   API_URL   default http://localhost:8000
//   API_KEY   default dev-local-key (app/core/config.py's Settings default)
//
// Per this project's bug history (5 of 6 bugs were in error/retry/
// concurrency/reporting paths, never the happy path), this script spends as
// much attention on the two REJECTION paths M5 adds — 401 from a bad API
// key, 429 from the sliding-window limiter — as it does on the happy path,
// and checks the exact response shape on each, not just the status code.

import http from "k6/http";
import { check, sleep } from "k6";

const API_URL = __ENV.API_URL || "http://localhost:8000";
const API_KEY = __ENV.API_KEY || "dev-local-key";

export const options = {
  scenarios: {
    // Ordinary traffic: create an account, authorize a payment, capture it.
    //
    // vus=1, not several: every VU authenticates as the SAME static key
    // (there's only one — ADR 0007 Decision 4), so concurrent VUs here
    // wouldn't model concurrent CALLERS, they'd model one caller's budget
    // getting divided by however many VUs k6 runs — discovered by first
    // running this at vus=5 and watching it legitimately rate-limit itself
    // into the 20-30% range purely from its own concurrency, nothing to do
    // with rate_limit_burst. One VU, paced with sleep(), is what "traffic
    // from one legitimate caller of the one key, comfortably under its own
    // 60-req/min budget" actually looks like under this design.
    happy_path: {
      executor: "constant-vus",
      exec: "happyPath",
      vus: 1,
      duration: "20s",
    },
    // A bad key on every request — the rejection path auth.py exists for.
    // Buckets on its own (wrong-key) string in the rate limiter, so it
    // never contends with happy_path's or rate_limit_burst's budget.
    unauthorized: {
      executor: "constant-vus",
      exec: "unauthorized",
      vus: 2,
      duration: "20s",
    },
    // Deliberately exceeds the per-key limit: same API key, no sleep, more
    // requests than rate_limit_max_requests (default 60) can admit inside
    // rate_limit_window_ms (default 60s) — must produce 429s, and the 429
    // body/header format must match app/api/rate_limit.py exactly.
    //
    // Scheduled to start only AFTER happy_path finishes, not concurrently
    // with it: this project has exactly one static API key (ADR 0007
    // Decision 4), so happy_path and a burst sharing that same key would
    // share the same rate-limit bucket and happy_path's own traffic would
    // get starved by this scenario's flood — a real, documented consequence
    // of the single-key design, not something a "separate test API key"
    // would be a fair workaround for in a k6 script that's meant to
    // exercise the ACTUAL single-key limitation, not paper over it.
    rate_limit_burst: {
      executor: "constant-vus",
      exec: "rateLimitBurst",
      vus: 20,
      duration: "15s",
      startTime: "22s",
    },
  },
  thresholds: {
    // Only the happy path is expected to be (near-)all 2xx. 401s and 429s
    // are the CORRECT outcome for the other two scenarios, not failures —
    // asserting a global error rate would make this test fight its own
    // purpose.
    "checks{scenario:happy_path}": ["rate>0.99"],
    "checks{scenario:unauthorized}": ["rate>0.99"],
  },
};

function headers(apiKey) {
  return { headers: { "Content-Type": "application/json", "X-API-Key": apiKey } };
}

function uniqueOwner(prefix) {
  return `${prefix}-${__VU}-${__ITER}-${Date.now()}`;
}

export function happyPath() {
  const payer = http.post(
    `${API_URL}/accounts`,
    JSON.stringify({
      owner_id: uniqueOwner("k6-payer"),
      name: "wallet",
      currency: "INR",
      allow_negative: true,
    }),
    headers(API_KEY),
  );
  const payerOk = check(payer, { "payer account created (201)": (r) => r.status === 201 });

  const payee = http.post(
    `${API_URL}/accounts`,
    JSON.stringify({ owner_id: uniqueOwner("k6-payee"), name: "wallet", currency: "INR" }),
    headers(API_KEY),
  );
  const payeeOk = check(payee, { "payee account created (201)": (r) => r.status === 201 });

  if (payerOk && payeeOk) {
    const payment = http.post(
      `${API_URL}/payments`,
      JSON.stringify({
        payer_account_id: payer.json("id"),
        payee_account_id: payee.json("id"),
        amount_minor: 1000,
      }),
      {
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": API_KEY,
          "Idempotency-Key": `k6-${__VU}-${__ITER}-${Date.now()}`,
        },
      },
    );
    check(payment, {
      "payment authorized (201)": (r) => r.status === 201,
      "payment status is authorized": (r) => r.json("status") === "authorized",
    });
  }

  sleep(2);
}

export function unauthorized() {
  const res = http.post(
    `${API_URL}/accounts`,
    JSON.stringify({ owner_id: uniqueOwner("k6-nope"), name: "wallet", currency: "INR" }),
    headers("definitely-the-wrong-key"),
  );
  check(res, {
    // app/api/auth.py's exact rejection shape: 401 with a "detail" string
    // (FastAPI's HTTPException body format) — asserted precisely, not just
    // "is it a 4xx", per this project's history of bugs that reported a
    // correct-looking outcome with the wrong shape.
    "bad key rejected (401)": (r) => r.status === 401,
    "bad key error detail is exact": (r) => r.json("detail") === "invalid API key",
  });

  sleep(1);
}

export function rateLimitBurst() {
  const res = http.post(
    `${API_URL}/accounts`,
    JSON.stringify({ owner_id: uniqueOwner("k6-burst"), name: "wallet", currency: "INR" }),
    headers(API_KEY),
  );
  check(res, {
    "burst response is 201 or 429, never anything else": (r) => r.status === 201 || r.status === 429,
  });
  if (res.status === 429) {
    check(res, {
      "429 body has the exact rate_limited shape": (r) =>
        r.json("error") === "rate_limited" && typeof r.json("retry_after_ms") === "number",
      "429 carries a numeric Retry-After header": (r) =>
        Number.isInteger(Number(r.headers["Retry-After"])) && Number(r.headers["Retry-After"]) >= 1,
    });
  }
  // No sleep — the point of this scenario is to exceed the window.
}
