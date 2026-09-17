# Empty secret CONTAINERS only. Terraform never writes a value into any of
# these — see ADR 0009's "Secrets: how they reach the running container"
# section. Values are set once, out-of-band, after `apply`:
#
#   aws secretsmanager put-secret-value \
#     --secret-id payments-platform/prod/database-url \
#     --secret-string 'postgresql+asyncpg://app:<password>@<rds-endpoint>:5432/payments'
#
# (the password comes from the RDS-managed secret rds.tf's
# manage_master_user_password created — `aws secretsmanager get-secret-value
# --secret-id <the ARN in the aws_db_instance.postgres output>`.)
#
# Names are `payments-platform/<environment>/<name>` so multiple
# environments (if this project ever gets a staging env) never collide on
# one secret.

locals {
  app_secrets = [
    "database-url",
    "redis-url",
    "kafka-bootstrap-servers",
    "kafka-sasl-username",
    "kafka-sasl-password",
    "api-key",
  ]
}

resource "aws_secretsmanager_secret" "app" {
  for_each = toset(local.app_secrets)

  name        = "payments-platform/${var.environment}/${each.value}"
  description = "payments-platform ${var.environment} — ${each.value}. Value set out-of-band; see this file's header comment."

  tags = { Name = "payments-platform-${var.environment}-${each.value}" }
}
