data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# --- ECS task execution role ---------------------------------------------------
# The role the ECS AGENT itself assumes to pull the image and resolve
# secrets before the container's own code ever runs — distinct from the
# task role below, which is what the application's own code would assume
# to call AWS APIs (it currently calls none, so that role stays empty).

resource "aws_iam_role" "ecs_task_execution" {
  name = "payments-platform-${var.environment}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The managed policy above covers ECR pull and CloudWatch Logs, but NOT
# Secrets Manager — that access is scoped by resource ARN here rather than
# granted via a second AWS-managed policy, which would be `secretsmanager:*`
# on `*`. This is the resource-scoped access ADR 0009's secrets section
# promises: the execution role can read exactly these six secrets and
# nothing else in the account.
resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name = "read-app-secrets"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [for s in aws_secretsmanager_secret.app : s.arn]
    }]
  })
}

# --- ECS task role ---------------------------------------------------------
# Empty today: app/core/config.py never calls an AWS API — it only reads
# environment variables the execution role above already resolved. This
# role exists now, with no policies attached, so a future need (e.g. an S3
# export job) is "attach a policy to an existing role," not "invent task
# roles for the first time under deploy pressure."
resource "aws_iam_role" "ecs_task" {
  name = "payments-platform-${var.environment}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# --- GitHub Actions OIDC ----------------------------------------------------
# See ADR 0009's secrets section: this replaces a long-lived
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY pair in GitHub Actions secrets with
# a role the workflow assumes for the duration of one job run, trusting
# tokens GitHub's own OIDC issuer signs — nothing to leak that outlives the
# run.

resource "aws_iam_openid_connect_provider" "github_actions" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's OIDC thumbprint is stable and documented; AWS also validates the
  # TLS chain independently of this value as of the provider's own default
  # behaviour, but the field is required.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions_deploy" {
  name = "payments-platform-${var.environment}-gha-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github_actions.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Restricts to THIS repo's `main` branch and its tag refs, not any
        # branch or any fork — a PR from a fork can never assume this role,
        # only a push to main or a `vX.Y.Z` tag on the repo itself (matching
        # .github/workflows/deploy.yml's own trigger).
        StringLike = {
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repo}:ref:refs/heads/main",
            "repo:${var.github_repo}:ref:refs/tags/*",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name = "deploy"
  role = aws_iam_role.github_actions_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*" # GetAuthorizationToken is account-wide by design — ECR has no resource-level scoping for it
      },
      {
        Sid    = "EcrPushToThisRepoOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid    = "DeployToEcs"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
        ]
        # RegisterTaskDefinition and DescribeTaskDefinition don't support
        # resource-level restriction (AWS requires "*" for these actions);
        # UpdateService/DescribeServices ARE scoped, immediately below.
        Resource = "*"
      },
      {
        Sid      = "DeployToThisClusterOnly"
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.main.name}/*"
      },
      {
        # Registering a task definition that references these roles requires
        # the caller be allowed to pass them to ECS — without this, a
        # RegisterTaskDefinition call that names either role fails. RunTask
        # (below) needs the identical permission for the same reason: it's
        # ECS itself assuming these roles on the caller's behalf to start a
        # task, not the caller assuming them directly.
        Sid      = "PassEcsRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.ecs_task_execution.arn, aws_iam_role.ecs_task.arn]
      },
      {
        # .github/workflows/deploy.yml's `migrate` job: `alembic upgrade
        # head` as a one-off task (a command override on the api task
        # definition), run and waited on BEFORE any service points at the
        # new revision. Scoped to the api family specifically (not "*",
        # like RegisterTaskDefinition/DescribeTaskDefinition above have to
        # be) and to this cluster only, via the ecs:cluster condition key —
        # this role can start a migration task, not an arbitrary task
        # definition on an arbitrary cluster in the account.
        Sid      = "RunMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.api.family}:*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = aws_ecs_cluster.main.arn
          }
        }
      },
      {
        # deploy.yml polls `aws ecs wait tasks-stopped` / `describe-tasks`
        # for that same migration task's exit code — DescribeTasks has no
        # task-definition-family scoping, only cluster, so this is as tight
        # as this action gets.
        Sid      = "DescribeMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:DescribeTasks"
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.main.name}/*"
      },
    ]
  })
}
