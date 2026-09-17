output "alb_dns_name" {
  description = "Public endpoint for the api service."
  value       = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  description = "Push images here (CI builds/pushes; see .github/workflows/deploy.yml)."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
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

output "github_actions_deploy_role_arn" {
  description = "Set as AWS_DEPLOY_ROLE_ARN in the GitHub repo's Actions variables (not a secret — the ARN itself isn't sensitive; assuming it requires the OIDC trust conditions in iam.tf to also match)."
  value       = aws_iam_role.github_actions_deploy.arn
}

output "app_secret_arns" {
  description = "ARNs to populate with `aws secretsmanager put-secret-value` before the first real deploy — see secrets.tf's header comment."
  value       = { for k, v in aws_secretsmanager_secret.app : k => v.arn }
}
