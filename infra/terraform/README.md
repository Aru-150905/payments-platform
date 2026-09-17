# infra/terraform

IaC for M7 (see `docs/adr/0009-deployment.md` for the reasoning behind every
choice below — this file is the "how to actually run it," not the "why").

## What this provisions

- **Network**: a VPC, 2 public + 2 private subnets across 2 AZs, no NAT
  Gateway (`network.tf`).
- **Postgres**: RDS, single-AZ, RDS-managed master password
  (`rds.tf`).
- **Redis**: a single-node ElastiCache cluster (`elasticache.tf`).
- **Kafka**: a Redpanda Cloud Serverless cluster and its three topics,
  provisioned via the `redpanda-data/redpanda` provider — the one resource
  in this directory that isn't in AWS at all (`redpanda.tf`).
- **Compute**: an ECS cluster running three independently-scaling Fargate
  services (api/relay/worker) off one container image, an ALB in front of
  the api only (`ecs.tf`, `alb.tf`).
- **Images**: an ECR repository with a lifecycle policy (`ecr.tf`).
- **Secrets**: empty Secrets Manager containers the app's task execution
  role can read; no value is ever written by Terraform (`secrets.tf`).
- **IAM**: the ECS task execution role, an (currently empty) task role, and
  a GitHub Actions OIDC deploy role scoped to this repo's `main` branch and
  tags (`iam.tf`).

## Before you run anything

Two providers, two sets of credentials, neither committed anywhere:

```bash
export AWS_ACCESS_KEY_ID=...       # or an AWS SSO / profile session
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...       # if using temporary credentials

export REDPANDA_CLIENT_ID=...      # or REDPANDA_ACCESS_TOKEN alone
export REDPANDA_CLIENT_SECRET=...
```

`terraform validate` needs neither — it's a pure syntax/reference check, no
API calls. `terraform plan` needs both, since it reads current state from
both AWS and Redpanda Cloud to compute a diff.

## Workflow

```bash
terraform init      # downloads providers; safe, no cloud calls, no cost
terraform validate  # syntax + reference check; safe, no cloud calls, no cost
terraform fmt -check -recursive

terraform plan \
  -var="redpanda_serverless_region=<real region — see below>" \
  -var="github_repo=<your-org>/<your-repo>"
  # read-only: queries current AWS/Redpanda state, shows a diff, changes
  # nothing. This is as far as this milestone goes — apply is a deliberate,
  # separate, reviewed step.
```

**`terraform apply` is intentionally not part of this milestone.** Per
CLAUDE.md's M7 scope: write the IaC, validate/plan it, stop. Everything
above is designed to be reviewable *before* a single dollar is spent —
`plan` shows exactly what would be created without creating it.

## Placeholder variables that must be set before a real `plan`/`apply`

| Variable                    | Why it's a placeholder                                                  |
|------------------------------|---------------------------------------------------------------------------|
| `redpanda_serverless_region` | Redpanda's own region names, not AWS's — look these up via the Redpanda Cloud console, or the `redpanda_serverless_regions` data source once you have credentials. |
| `github_repo`                | Scopes the OIDC trust policy (`iam.tf`) to your fork/repo, not this project's. |
| `container_image`            | The CI/CD workflow (`.github/workflows/deploy.yml`) passes this per deploy; only needed by hand for a one-off manual `plan`/`apply`. |

## After a real `apply`: populating secrets

Terraform creates the Secrets Manager *containers*, never their values (see
`secrets.tf`'s header comment and ADR 0009's secrets section). One-time,
by hand, after `apply`:

```bash
# Postgres password comes from RDS's own managed secret — read it once:
aws secretsmanager get-secret-value \
  --secret-id "$(terraform output -raw rds_master_user_secret_arn)" \
  --query SecretString --output text

aws secretsmanager put-secret-value \
  --secret-id payments-platform/prod/database-url \
  --secret-string "postgresql+asyncpg://app:<password-from-above>@$(terraform output -raw rds_endpoint)/payments"

aws secretsmanager put-secret-value \
  --secret-id payments-platform/prod/redis-url \
  --secret-string "redis://$(terraform output -raw redis_endpoint):6379/0"

aws secretsmanager put-secret-value \
  --secret-id payments-platform/prod/kafka-bootstrap-servers \
  --secret-string "$(terraform output -json kafka_bootstrap_servers | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)))')"

# kafka-sasl-username / kafka-sasl-password: from the Redpanda Cloud console
# (Serverless cluster -> Security -> SASL users) or `redpanda_user` if you
# choose to manage the app's Kafka user in Terraform too (not done here —
# credential VALUES belong out of Terraform state the same way the app's
# own secrets do).

aws secretsmanager put-secret-value \
  --secret-id payments-platform/prod/api-key \
  --secret-string "$(openssl rand -hex 32)"
```

Then set these GitHub repo Actions **variables** (Settings -> Actions ->
Variables — not Secrets, since none of these values are sensitive, they're
resource identifiers) from this `apply`'s outputs, once:

| Repo variable          | `terraform output` |
|-------------------------|----------------------|
| `AWS_REGION`             | (your `var.aws_region` value) |
| `AWS_DEPLOY_ROLE_ARN`    | `github_actions_deploy_role_arn` |
| `ECR_REPOSITORY_NAME`    | `ecr_repository_name` |
| `ECS_CLUSTER_NAME`       | `ecs_cluster_name` |
| `ECS_ENVIRONMENT`        | (your `var.environment` value, e.g. `prod`) |
| `ECS_SUBNET_IDS`         | `ecs_task_subnet_ids` |
| `ECS_SECURITY_GROUP_ID`  | `ecs_tasks_security_group_id` |

`.github/workflows/deploy.yml` reads every one of these; it fails fast (a
missing `vars.*` renders as an empty string in the workflow, which the AWS
CLI calls it feeds into reject immediately) rather than deploying against a
guessed identifier if any is unset. Once they're set, push a tag
(`git tag v0.1.0 && git push origin v0.1.0`) to run it.

## Follow-ups not done in this milestone

- **HTTPS/ACM**: the ALB listener (`alb.tf`) is plain HTTP on :80 — no
  registered domain or ACM certificate to terminate TLS with yet. Add an
  `aws_acm_certificate` + HTTPS listener once a domain exists; redirect :80
  to :443 at that point rather than leaving both open.
- **Remote state**: `versions.tf` uses the local backend deliberately (see
  its comment) — state lives in this directory, gitignored. Move to an S3
  backend with a DynamoDB lock table before more than one person ever runs
  `apply` against this.
- **Multi-AZ RDS / Redis replica**: both off by default
  (`db_multi_az = false`, single ElastiCache node) — see `variables.tf` and
  `elasticache.tf`'s comments on why that's the right default for a
  portfolio deployment and the wrong one once anything real depends on
  uptime.

## Runbook

Operational procedures (consumer lag, the DLQ, replay, rollback, what each
alert means) live in `docs/runbook.md`, not here — this file is about
provisioning the infrastructure; that one is about operating what's running
on it.
