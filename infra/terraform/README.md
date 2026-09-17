# infra/terraform

IaC for M7 (`docs/adr/0009-deployment.md`). One flat module — no
submodules — matching this project's "no premature abstraction" rule; the
resource count here doesn't justify splitting it further.

## What's here

| File                 | What it creates                                                        |
| -------------------- | ----------------------------------------------------------------------- |
| `network.tf`          | VPC, public + private subnets across 2 AZs, no NAT Gateway (see its own comment for why) |
| `security_groups.tf`  | ALB, ECS tasks, RDS, Redis — least-privilege ingress                    |
| `rds.tf`              | Postgres 16, RDS-managed master password                                |
| `elasticache.tf`      | Redis, single node                                                      |
| `ecr.tf`               | Container image repository + lifecycle policy                          |
| `secrets.tf`           | Empty Secrets Manager containers (no values — see below)                |
| `iam.tf`               | ECS task/execution roles, GitHub Actions OIDC provider + deploy role    |
| `alb.tf`               | Public ALB in front of the api service only                             |
| `ecs.tf`               | Cluster, 3 task definitions + 3 services (api/relay/worker), api autoscaling |
| `outputs.tf`           | Everything you need to finish setup after `apply`                       |

## Status: validated, not applied

This milestone's instructions were explicit: write the IaC, validate it,
stop before `apply`. What was actually run in this environment:

```
terraform fmt      # ok
terraform init     # ok
terraform validate # ok — catches type errors, invalid attribute combos,
                    #     and AWS API constraints (e.g. this config
                    #     originally had em dashes in two security group
                    #     descriptions, which AWS's API rejects; validate
                    #     caught it before a human would have)
terraform plan     # fails on "No valid credential sources found" —
                    #     this environment has no AWS credentials at all
                    #     (no ~/.aws, no AWS CLI installed). This is the
                    #     correct stopping point per the milestone's own
                    #     instructions, not a bug to route around.
```

A real `plan` (and eventually `apply`) needs AWS credentials this
environment intentionally doesn't have. Nothing above ever calls an AWS API
that creates, modifies, or costs anything.

## Before you `apply` for real

1. `cp terraform.tfvars.example terraform.tfvars`, fill in `github_repo` at
   minimum.
2. Switch the backend to remote state (`versions.tf`'s commented-out `s3`
   block) — the local backend here is fine for reviewing a plan, not for
   anything you intend to keep.
3. `terraform init && terraform plan` with real AWS credentials — read the
   plan. It should create ~35 resources and destroy nothing.
4. `terraform apply`.
5. Read the RDS-managed master password:
   ```
   aws secretsmanager get-secret-value \
     --secret-id "$(terraform output -raw rds_master_user_secret_arn)" \
     --query SecretString --output text
   ```
6. Populate the six app secrets (`secrets.tf`'s header comment has the exact
   command). `database-url` and `redis-url` are assembled from the RDS/Redis
   endpoints (`terraform output rds_endpoint` / `redis_endpoint`) plus the
   password from step 5; the three Kafka values come from your Redpanda
   Cloud Serverless cluster's connection details; `api-key` is any secret
   string you generate (`openssl rand -hex 32`).
7. Run migrations once against the new database (from a one-off ECS task,
   or locally with `DATABASE_URL` pointed at the RDS endpoint through an SSH
   tunnel/bastion — the RDS security group only admits the ECS tasks' SG,
   so nothing else can reach it directly by design).
8. Set the GitHub repo's Actions variable `AWS_DEPLOY_ROLE_ARN` to
   `terraform output github_actions_deploy_role_arn`, and `AWS_REGION` to
   your chosen region. `.github/workflows/deploy.yml` reads both.
9. Push a `vX.Y.Z` tag. See `docs/runbook.md` for what happens next and how
   to roll it back if it goes wrong.

## Known follow-ups (not this milestone)

- **HTTPS**: the ALB listens on plain HTTP (`alb.tf`). Needs a registered
  domain and an ACM certificate before this is anything more than a
  reviewable dry run — deliberately out of scope here.
- **NAT Gateway**: `network.tf` explains the cost tradeoff of running ECS
  tasks in public subnets instead. Revisit if this ever carries real
  traffic.
- **Redpanda Cloud cluster itself**: not provisioned by this Terraform. Its
  provider requires an API token this environment doesn't have either, and
  a Serverless cluster is created once via the Redpanda Cloud console/API,
  not something this project's IaC needs to own the lifecycle of — the
  Terraform here only creates the *consumer* of its connection details
  (the `kafka-*` secrets).
