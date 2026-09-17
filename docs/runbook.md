# Runbook

Operational procedures for payments-platform in production (M7). This is
about what to *do* when something needs attention; see
`docs/adr/0009-deployment.md` for *why* the infrastructure is shaped this
way, and `infra/terraform/README.md` for how to actually provision it.

**The three deployed processes**, per ADR 0003/ADR 0009: `api` (serves
HTTP, ECS service `api`), `relay` (drains the outbox to Kafka, ECS service
`relay`), `worker` (the read-model consumer, ECS service `worker`). They
scale and fail independently — a problem in one does not imply a problem in
the others, so the first question for any alert is *which* process it's
about.

---

## Checking consumer lag

`kafka_consumer_lag{topic,partition,group}` (ADR 0007 Decision 3) is the
metric that matters most: the api can report perfectly healthy while the
`worker` is dead or wedged and `read_model_payments` silently goes stale.
There is currently one consumer group, `payments-read-model-v1`
(`settings.read_model_consumer_group`), reading `payments.payment.v1`.

**Locally** (docker-compose stack):

```bash
# Raw offsets/lag, per partition:
docker exec pp-kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --group payments-read-model-v1

# Or the metric itself, straight from the worker process:
curl -s localhost:9102/metrics | grep kafka_consumer_lag

# Or visually: `make observability`, then Grafana at localhost:3000 ->
# "Kafka consumer lag — the metric that matters" panel
# (observability/grafana/dashboards/m5-overview.json).
```

**In the deployed environment**: no Prometheus/Grafana is deployed to AWS
in this milestone (`infra/terraform/` provisions the application stack
only — see its README's follow-ups). The authoritative source of lag for a
Redpanda Cloud Serverless cluster is Redpanda's own control plane, via
`rpk` (Redpanda's CLI, speaks the same Kafka protocol the app does):

```bash
rpk group describe payments-read-model-v1 \
  --brokers "$KAFKA_BOOTSTRAP_SERVERS" \
  -X user="$KAFKA_SASL_USERNAME" -X pass="$KAFKA_SASL_PASSWORD" \
  -X sasl.mechanism=SCRAM-SHA-256 -X tls.enabled=true
```

(credentials from the `kafka-sasl-username`/`kafka-sasl-password` secrets —
`aws secretsmanager get-secret-value`, same as `infra/terraform/README.md`'s
setup steps). The Redpanda Cloud console shows the same thing under the
cluster's Consumer Groups view, no CLI needed.

**What's normal**: lag should hover near 0 and only bump briefly right
after a rolling deploy (the `worker` service's `deployment_minimum_healthy_
percent = 100` means a new task starts before the old one stops — a few
seconds of two-consumer rebalance, not a real backlog). `alerts.yml`'s
`KafkaConsumerLagHigh` (>1000 for 10m) and `KafkaConsumerLagCritical`
(>10000 for 10m) are the two thresholds worth reacting to; see "What each
alert means" below for what to do about each.

**If lag is climbing and not recovering**: check the worker's own health
first (`aws ecs describe-services --cluster <cluster> --services worker` —
is the task actually running, how many restarts) and its logs
(CloudWatch Logs group `/ecs/payments-platform-<env>`, log stream prefixed
`worker`) for exceptions. A worker that's up but not making progress is
almost always stuck on the same poison message on every poll — see the DLQ
section immediately below, because right now that's the *only* way a
stalled consumer manifests.

---

## The DLQ: current state and what to do when it fills

**Read this section's first two paragraphs before assuming the DLQ has
data in it.** `payments.dlq.v1` exists as a topic (`app/events/topics.py`,
provisioned in both `Makefile`'s `topics` target and
`infra/terraform/redpanda.tf`), but **nothing in this codebase publishes to
it yet**. `app/events/consumer.py`'s `drain_once()` catches any exception
from `handle()`, logs it, and moves on to the next message in the batch —
its own comment says exactly this: *"M3 routes this to the DLQ after
bounded retries. For now, log and move on rather than block the partition
forever on one poison message."* M3 (retries with backoff, DLQ routing,
a replay CLI) is still open on the roadmap in `CLAUDE.md` as of this
milestone. This runbook describes both the real, current behavior and the
intended one, clearly separated, rather than documenting a mechanism that
doesn't exist yet.

### What actually happens today when a message can't be processed

The worker logs `handler failed offset=<N>` with a full traceback
(`log.exception` in `drain_once()`), **commits the offset anyway** (commit
happens unconditionally after the batch, not conditionally on every message
succeeding), and moves on. Consequences:

- The event is **not lost from Kafka** — it's still on
  `payments.payment.v1` at that offset, for as long as the topic's
  retention window holds it (7 days, `kafka_topic_retention_hours`).
- It **is** permanently skipped by this consumer group specifically — the
  committed offset means `payments-read-model-v1` will never see it again
  on a normal `run()`, only on a full `rebuild()`.
- `read_model_payments` is now missing (or stale for) whatever that event
  would have projected. Nothing surfaces this automatically today — no
  metric increments, no alert fires. The only current signal is the log
  line itself.

### Detecting it today

```bash
# CloudWatch Logs Insights, against the worker's log stream:
fields @timestamp, @message
| filter @message like /handler failed offset=/
| sort @timestamp desc
```

Treat any hit as needing manual investigation: read the traceback, find the
event (it's still on the topic — use `kafka-console-consumer.sh`/`rpk topic
consume payments.payment.v1 --offset <N>-<N+1>` to read that exact offset
locally or against Redpanda Cloud), decide whether it's a genuinely bad
event (fix the data, or accept the loss) or a bug in `read_model.apply_event`
(fix the code, then rebuild).

### Recovering today: full rebuild, not targeted replay

There is no way today to replay *just* the one bad offset without also
reprocessing everything before it. The tool that exists is a full rebuild:

```bash
make rebuild-read-model   # or: python -m scripts.rebuild_read_model
```

This truncates `read_model_payments`, forgets `payments-read-model-v1`'s
dedup history, and replays `payments.payment.v1` from offset 0
(`app/events/consumer.py`'s `rebuild()`). **Do not run this while the
`worker` service has running tasks** — `desired_count` to 0 first
(`aws ecs update-service --cluster <cluster> --service worker
--desired-count 0`, then back to its original value after), or two
consumers in the same group will fight over the reset offsets. If the
underlying bug is in `apply_event` itself, fix and deploy that first, or
rebuild will just reproduce the same failure at the same offset.

### The intended procedure, once M3 lands

`drain_once()` gets bounded retries, then a publish to `payments.dlq.v1`
(envelope plus original topic/partition/offset and the error) instead of a
silent commit-and-skip. At that point "the DLQ fills" becomes a real,
observable condition (consumer lag on `payments.dlq.v1` itself, or a
count-based alert on messages landing there), and replay becomes targeted:
a small consumer that reads `payments.dlq.v1` and republishes each message
back to its original topic, letting the normal consumer group reprocess it
in offset order rather than requiring a full rebuild. Until that lands, the
procedure above is the real one.

---

## Rolling back a deploy

**Never `terraform apply` to roll back, and never `git revert` + re-tag.**
`infra/terraform/ecs.tf`'s `aws_ecs_service` resources all set
`lifecycle { ignore_changes = [task_definition] }` specifically so that
Terraform never fights the ECS service over which task-definition revision
is currently running — deploys are owned entirely by
`.github/workflows/deploy.yml`, and rollback is the same mechanism run
backwards: point each service at a previously-registered, already-working
task definition revision.

Every deploy (`deploy.yml`) registers a **new** task-definition revision
per service rather than mutating an existing one — ECS keeps every past
revision registered (inactive ones don't cost anything and aren't deleted
automatically), so "the previous version" always still exists and is one
`update-service` call away:

```bash
CLUSTER=payments-platform-prod   # infra/terraform's ECS_CLUSTER_NAME output/var

for SERVICE in api relay worker; do
  FAMILY="payments-platform-prod-${SERVICE}"

  CURRENT=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].taskDefinition' --output text)
  REVISION="${CURRENT##*:}"
  PREVIOUS="${FAMILY}:$((REVISION - 1))"

  echo "$SERVICE: $CURRENT -> $PREVIOUS"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$PREVIOUS" --force-new-deployment
done

aws ecs wait services-stable --cluster "$CLUSTER" --services api relay worker
```

Roll back `worker` and `relay` before `api` if the bad deploy is
Kafka/outbox-related and you want to stop the bleeding fastest; order
doesn't otherwise matter since ADR 0002/ADR 0006 already make every
consumer tolerant of the other two processes being briefly on different
code versions during a rolling update.

**The one case this doesn't cleanly cover**: a deploy whose `alembic
upgrade head` step made a *destructive* schema change (dropped a column a
previous revision's code still reads, for example). Rolling the code back
does not roll the schema back — `deploy.yml`'s `migrate` job never runs
`alembic downgrade`, on purpose, since a downgrade that already lost data
(a dropped column) can't actually restore it. The real fix for this case is
prevention, not rollback: write migrations that are additive-first
(add the new column, deploy code that uses it, *then* a later deploy drops
the old one), the same "expand/contract" discipline that makes any rolling
deploy safe regardless of platform.

**To redeploy from source instead** (rolling forward to a known-good older
commit rather than pointing at an already-built image): tag that commit
with a new tag (`git tag v1.4.1 <old-good-sha> && git push --tags`) and let
`deploy.yml` run normally — this rebuilds and re-migrates, which is slower
but exercises the exact same path a forward deploy does, at the cost of a
fresh `alembic upgrade head` against a database that's already at head
(a no-op, safe) rather than the instant `update-service` path above.

---

## What each alert means

`observability/prometheus/alerts.yml`. No Alertmanager is deployed (no
paging/Slack routing — single operator, no on-call rotation); these are
checked by hand in Prometheus's own `/alerts` page or Grafana, not pushed
to anyone. Locally, `make observability` runs Prometheus with this rule
file loaded.

| Alert | Fires when | What it means | What to check |
|---|---|---|---|
| `HighHttpErrorRate` | >5% of `http_requests_total` are 5xx, sustained 5m | The api is failing requests at an elevated rate | `curl .../health/ready`; recent api logs in CloudWatch; is Postgres reachable |
| `HighHttpLatencyP95` | p95 of `http_request_duration_seconds` >1s, sustained 5m | Requests are slow — likely DB contention or CPU starvation, *not* Kafka (the api never calls Kafka directly, ADR 0002) | Postgres connection pool metrics/slow query log; api task CPU in ECS; whether api's autoscaling (`ecs.tf`) has actually scaled out |
| `KafkaCircuitBreakerOpen` | `kafka_circuit_breaker_state{component="relay_producer"} == 2` | The relay's producer breaker has tripped after 5 consecutive publish failures (ADR 0007 Decision 2) — it stops trying to reach Kafka for `recovery_timeout_s` (30s) at a time | Is Redpanda Cloud reachable/up (its own status page); relay logs for the underlying connection error; outbox row count growing (`SELECT count(*) FROM outbox_events WHERE published_at IS NULL`) confirms writes are still succeeding and just queuing, per ADR 0002's design |
| `KafkaConsumerLagHigh` | worker's group lag >1000, sustained 10m | Falling behind — could be a genuine slowdown or the start of a stall | "Checking consumer lag" above; worker task health/restart count |
| `KafkaConsumerLagCritical` | worker's group lag >10000, sustained 10m | Very likely dead or stuck on a poison message, not just slow | Same as above, treat as urgent; check for `handler failed offset=` in worker logs — see the DLQ section |

None of these check Kafka from the api's own `/health/ready` — that's
deliberate (`app/main.py`'s own docstring): the outbox is what lets the api
keep accepting writes through a Kafka outage, so a readiness probe that
failed on Kafka being down would throw away the exact resilience ADR 0002
was built for. `KafkaCircuitBreakerOpen` and the two lag alerts are how a
Kafka-side problem actually surfaces instead — through the relay/worker's
own metrics, not the api's health check.
