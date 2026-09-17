# ADR 0009 — Deployment: managed Kafka, compute shape, and secrets (M7)

**Status:** accepted · **Date:** 2026-09

## Context

Everything through M6 runs on a laptop: `docker compose` for Postgres, Redis,
and a single-node KRaft Kafka broker, three Python processes started by hand
(`make api`/`relay`/`worker`). M7 has to turn that into something a second
person could actually deploy: infrastructure as code, a container image, a
CI/CD pipeline, and a runbook. Four decisions have real alternatives worth
recording.

## Decision 1: Terraform for IaC

The alternatives are AWS CDK/Pulumi (real code, but couples the infra
definition to a language runtime and its own dependency tree — one more
thing to keep patched) and hand-run `aws` CLI commands (not IaC at all: no
diff, no plan, no record of what "the infrastructure" is supposed to be).
Terraform's HCL is declarative, its plan step is exactly the dry-run this
milestone requires before anything real gets created, and it is the tool an
interviewer is most likely to have used themselves. `infra/terraform/` holds
it; see that directory's own README for the module layout.

## Decision 2: Redpanda Cloud (Serverless) over MSK and over self-managed

The three real options, and what they actually cost to run:

**Self-managed Kafka on EC2** was rejected for the same reason ADR 0003
rejected microservices and Kubernetes: it adds real operational surface —
patching broker hosts, managing disk and replication, sizing JVM heaps,
handling broker failure and rebalance — for a system with one operator and no
on-call rotation. The entire point of *choosing* a managed option is to not
own that; picking self-managed here would be paying the complexity cost of
"managed Kafka" without buying the thing it's for. `docker-compose.yml`'s own
comment already says the single-broker KRaft setup there is "fine for local
dev, never for prod" — this is that line's payoff.

**Amazon MSK** is the AWS-native answer and the safer choice if this system
already lived inside a large AWS estate with a platform team managing shared
VPC infrastructure. Its problems here are cost floor and network surface, not
capability: MSK has no serverless-to-zero tier for a provisioned cluster —
the minimum viable production setup is 3 brokers (one per AZ, so losing an AZ
doesn't lose quorum) at `kafka.m7g.large` or similar, billed by the hour
whether or not a single message is produced, plus EBS storage per broker.
That is real money for a portfolio project's idle time, which is most of its
life. It also requires the cluster to sit inside a VPC with subnets, security
groups, and either public NAT or VPC peering/PrivateLink for anything outside
that VPC to reach it — networking surface this project's compute layer
(Decision 3) would otherwise not need at all.

**Redpanda Cloud, Serverless tier**, is the pick. It is Kafka
wire-protocol-compatible — `aiokafka` in `app/events/producer.py` and
`consumer.py` talks to it unmodified, only `kafka_bootstrap_servers` plus
SASL/TLS credentials change (see `app/core/config.py`). Serverless has no
per-broker hourly floor: it bills metered, has a free allowance sized for
exactly this project's traffic (three topics, single-digit partitions, no
sustained production load), and is reachable over a public SASL_SSL endpoint
— no VPC peering needed, which keeps the compute layer's networking as simple
as "ECS tasks with a NAT egress path," not "ECS tasks plus a Kafka-specific
peering connection." The tradeoff being accepted: Serverless is a shared
multi-tenant cluster with throughput ceilings and fewer guarantees than a
dedicated one. The upgrade path when that stops being true is Redpanda
Cloud's **BYOC/Dedicated** tier — brokers in this AWS account's own VPC,
same wire protocol, same client code, only the Terraform network module and
the bootstrap-servers value change. That upgrade is a config change, not a
rewrite, which is what makes accepting Serverless's limits now a reasonable
bet rather than a trap.

## Decision 3: ECS Fargate, three services, not one

ADR 0003's whole argument was: one codebase, one database, but the API, the
relay, and the read-model consumer are separate *processes* because they
scale and fail independently — a slow consumer should never be able to steal
CPU from the process answering HTTP requests, and a traffic spike on the API
should never starve the relay of the CPU it needs to keep outbox lag low.
That argument is about processes, not about where they run, so it carries
over unchanged: `infra/terraform/ecs.tf` defines three ECS *services* (api,
relay, worker), each with its own task definition, its own desired count,
and its own CPU/memory reservation, all built from the **same container
image** (Decision 4) with only the container `command` differing. Scaling the
API to handle a traffic spike does not touch the relay or worker task
counts, and vice versa — the same independence the milestone asks for, now
enforced by the orchestrator instead of by "these happen to be different
`make` targets."

Fargate over EC2-backed ECS or Kubernetes: EKS is explicitly ruled out by
ADR 0003 ("do not introduce microservices, Kubernetes, or a service mesh"),
and this system was never going to become one just because the process count
grew from three `make` targets to three deployed services. EC2-backed ECS
would mean patching and right-sizing the underlying instances ourselves —
the same "managed vs. self-managed" tradeoff as Decision 2, decided the same
way: Fargate's per-task billing with no host to patch is the smaller
operational surface for one operator, at a real per-vCPU-hour premium over
bare EC2 that is worth paying at this scale.

## Decision 4: one multi-stage Dockerfile, three commands

`Dockerfile` builds a single image: a `builder` stage installs
`requirements.txt` into a virtualenv with a C toolchain available (`asyncpg`
and some of its transitive dependencies compile from source on some
platforms), then a `runtime` stage copies only the built virtualenv and the
`app/` and `migrations/` source trees onto a slim Python base — no compiler,
no `requirements-dev.txt` (no `pytest`, `ruff`, or `httpx`), no `.git`. The
three ECS services all reference this same image and same ECR repository;
`ecs.tf` sets each task definition's `command` to what `make api` / `make
relay` / `make worker` already run locally (`uvicorn app.main:app ...`,
`python -m app.events.relay`, `python -m app.events.consumer`). One image
build, three deployments, matching the "same codebase, separate processes"
framing of Decision 3 and ADR 0003 exactly — building three separate images
would suggest they're three separate applications, which is the one thing
ADR 0003 says they explicitly are not.

## Secrets: how they reach the running container

None of `DATABASE_URL`, `REDIS_URL`, the Kafka SASL credentials, or
`API_KEY` are ever written to a file in this repository, a Docker image
layer, or Terraform state as a literal value. The flow:

1. `infra/terraform/secrets.tf` creates empty **AWS Secrets Manager** secret
   containers (name and description only). Terraform never writes a secret
   *value* — `terraform plan`/`apply` for this file touches only the
   container resource, so a value never appears in a plan diff, in state, or
   in this repo's history. Values are set once, out-of-band, with `aws
   secretsmanager put-secret-value` (or the console) after `apply`, by
   whoever holds production access.
2. Each ECS task definition's container definition references those ARNs
   under `secrets` (not `environment`) — the standard ECS mechanism where
   the container agent resolves the secret and injects it as an environment
   variable *at container start*, inside the running task, never baked into
   the task definition's own JSON as a value.
3. `app/core/config.py` is untouched: it already reads every setting from an
   environment variable and nothing else (`DATABASE_URL` is exactly what
   `Settings.database_url` expects), which is the reason this M7 work needed
   zero application-code changes to become deployable — that file's own
   docstring says config lives there "so that the same image can run on your
   laptop and on AWS with only env vars changing," and this is that promise
   being cashed in.
4. The task execution role (`iam.tf`) is the only principal allowed
   `secretsmanager:GetSecretValue` on these ARNs, scoped by resource, not
   `*`.
5. **CI/CD → AWS**: GitHub Actions authenticates via **OIDC federation**
   (`iam.tf`'s `aws_iam_openid_connect_provider` + a deploy role trusting
   only this repo's GitHub Actions issuer), not a long-lived
   `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` pair sitting in GitHub
   secrets. A leaked GitHub secret under this scheme is a short-lived,
   repo-scoped token, not a permanent AWS credential.

## Alternatives rejected

- **Self-managed Kafka, MSK provisioned (non-serverless)** — see Decision 2.
- **EKS / EC2-backed ECS** — see Decision 3; also ADR 0003.
- **Separate container images per process** — see Decision 4.
- **`.env` file baked into the image, or plaintext env vars in the task
  definition** — the two most common ways secrets leak into logs, image
  layers, or `terraform show` output. Rejected outright; Secrets Manager +
  ECS's `secrets` field is the standard mechanism for exactly this reason.
- **Long-lived AWS IAM user keys in GitHub Actions secrets** — works, but a
  static credential that never expires and isn't scoped to a specific repo
  or workflow run is a worse blast radius for the same convenience OIDC
  gives for free.

## Consequences

- Redpanda Serverless's throughput ceilings mean a genuine production launch
  (not a portfolio demo) would need to budget for the BYOC tier upgrade —
  flagged in Decision 2, not solved here.
- Fargate's per-vCPU-hour price is higher than the equivalent EC2 capacity
  would be; accepted because this project has no one to patch EC2 hosts.
- Nothing in `infra/terraform/` is applied by this milestone. `terraform
  plan` (or `validate`, where `plan` needs credentials this environment does
  not have) is the artifact this ADR's decisions are checked against; a
  human runs `apply` after reviewing that plan. See
  `infra/terraform/README.md`.
- Secrets Manager and the OIDC deploy role both need to exist and be
  populated *before* the first real deploy — the CI/CD workflow's deploy job
  will fail cleanly (assume-role or GetSecretValue denied) rather than
  deploy with an empty credential if that setup is skipped.
