output "alb_dns_name" {
  description = "Public endpoint for the api service."
  value       = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  description = "Push images here (CI builds/pushes; see .github/workflows/deploy.yml)."
  value       = aws_ecr_repository.app.repository_url
}

output "ecr_repository_name" {
  description = "Set as the ECR_REPOSITORY_NAME repo variable — deploy.yml needs the bare name, not the full URL amazon-ecr-login already provides the registry half of."
  value       = aws_ecr_repository.app.name
}

output "ecs_cluster_name" {
  description = "Set as the ECS_CLUSTER_NAME repo variable (.github/workflows/deploy.yml)."
  value       = aws_ecs_cluster.main.name
}

output "ecs_task_subnet_ids" {
  description = "Set as the ECS_SUBNET_IDS repo variable, comma-joined — deploy.yml's one-off migration task runs here, same subnets the services themselves use (see network.tf's file-level comment on why these are public, not private, subnets)."
  value       = join(",", aws_subnet.public[*].id)
}

output "ecs_tasks_security_group_id" {
  description = "Set as the ECS_SECURITY_GROUP_ID repo variable — the same SG every api/relay/worker task already runs under (security_groups.tf)."
  value       = aws_security_group.ecs_tasks.id
}

output "rds_endpoint" {
  description = "Host:port for DATABASE_URL. The password comes from the RDS-managed secret below, not from here."
  value       = aws_db_instance.postgres.endpoint
}

output "rds_master_user_secret_arn" {
  description = "RDS-managed Secrets Manager ARN holding the generated master password (rds.tf's manage_master_user_password) — read this once to assemble the app's own database-url secret (secrets.tf)."
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "kafka_bootstrap_servers" {
  description = "Redpanda Serverless seed brokers — populate the kafka-bootstrap-servers secret (secrets.tf) with this, comma-joined, as KAFKA_BOOTSTRAP_SERVERS expects (app/core/config.py)."
  value       = redpanda_serverless_cluster.main.kafka_api.seed_brokers
}

output "github_actions_deploy_role_arn" {
  description = "Set as AWS_DEPLOY_ROLE_ARN in the GitHub repo's Actions variables (not a secret — the ARN itself isn't sensitive; assuming it requires the OIDC trust conditions in iam.tf to also match)."
  value       = aws_iam_role.github_actions_deploy.arn
}

output "app_secret_arns" {
  description = "ARNs to populate with `aws secretsmanager put-secret-value` before the first real deploy — see secrets.tf's header comment."
  value       = { for k, v in aws_secretsmanager_secret.app : k => v.arn }
}
