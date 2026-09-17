variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Deployment environment name, used in resource names and tags."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = <<-EOT
    Number of availability zones to spread subnets across. 2, not 1 or 3:
    RDS and ElastiCache subnet groups require at least 2 AZs to accept a
    Multi-AZ upgrade later without a subnet group replacement, and a third
    AZ buys no extra durability for a single-instance dev/portfolio
    deployment that isn't paying for Multi-AZ in the first place.
  EOT
  type        = number
  default     = 2
}

variable "container_image" {
  description = <<-EOT
    Full image reference (ECR repo URL + tag) the ECS task definitions run.
    Left unset here on purpose — the CI/CD workflow passes it explicitly per
    deploy (the git SHA it just built and pushed), so `terraform apply`
    never silently redeploys "whatever :latest happens to point to right
    now." Plan/validate runs still work with the placeholder default.
  EOT
  type        = string
  default     = "REPLACE_ME:unset"
}

# --- Postgres (RDS) ----------------------------------------------------------

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the smallest ARM (Graviton) class — right-sized for portfolio-scale traffic, cheapest that still gets burstable CPU credits rather than a hard throttle."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "RDS allocated storage in GB. gp3 minimum useful size; this project's data volume is nowhere near it."
  type        = number
  default     = 20
}

variable "db_multi_az" {
  description = "Whether RDS runs a synchronous standby in a second AZ. False by default — doubles the instance cost for failover protection a portfolio deployment doesn't need; flip to true before anything with real users depends on this."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 3
}

# --- Redis (ElastiCache) ------------------------------------------------------

variable "redis_node_type" {
  description = "ElastiCache node type. cache.t4g.micro mirrors the RDS sizing rationale above."
  type        = string
  default     = "cache.t4g.micro"
}

# --- ECS task sizing -----------------------------------------------------------
# Fargate bills per vCPU/memory-second per task, so these three are sized
# independently per ADR 0009 Decision 3 rather than sharing one number — the
# relay and worker are lightweight polling loops; the api serves HTTP
# concurrently and gets more headroom.

variable "api_cpu" {
  type    = number
  default = 512 # 0.5 vCPU
}

variable "api_memory" {
  type    = number
  default = 1024 # MiB
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "relay_cpu" {
  type    = number
  default = 256
}

variable "relay_memory" {
  type    = number
  default = 512
}

variable "relay_desired_count" {
  type    = number
  default = 1
}

variable "worker_cpu" {
  type    = number
  default = 256
}

variable "worker_memory" {
  type    = number
  default = 512
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

# --- Kafka (Redpanda Cloud Serverless) ------------------------------------------

variable "redpanda_serverless_region" {
  description = <<-EOT
    Redpanda Cloud Serverless region name — Redpanda's own region
    identifiers, NOT an AWS region string, and not necessarily the same
    value as var.aws_region. The real list requires the
    redpanda_serverless_regions data source (needs REDPANDA_CLIENT_ID/
    REDPANDA_CLIENT_SECRET, which this environment doesn't have) or the
    Redpanda Cloud console. `validate` works with this placeholder;
    `plan`/`apply` need the real value set first.
  EOT
  type        = string
  default     = "REPLACE_ME"
}

# --- CI/CD OIDC ----------------------------------------------------------------

variable "github_repo" {
  description = <<-EOT
    "<owner>/<repo>", used to scope the GitHub Actions OIDC trust policy so
    only workflow runs from this exact repository can assume the deploy
    role. Left as a placeholder default so `validate`/`plan` work without
    the reviewer needing to know the real repo slug up front — set the real
    value before `apply`.
  EOT
  type        = string
  default     = "REPLACE_ME/payments-platform"
}
