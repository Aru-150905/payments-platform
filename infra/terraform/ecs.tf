resource "aws_ecs_cluster" "main" {
  name = "payments-platform-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "disabled" # a per-cluster CloudWatch cost with no reviewer benefit for a dry-run plan; turn on if this ever runs for real
  }
}

resource "aws_cloudwatch_log_group" "app" {
  # One group, not three — CloudWatch Logs groups aren't billed per group,
  # and one group with a `service` field per stream is one place to look,
  # not three, when correlating what the api was doing against what the
  # relay was doing at the same wall-clock moment (exactly the kind of
  # cross-process question docs/runbook.md's debugging steps need to answer).
  name              = "/ecs/payments-platform-${var.environment}"
  retention_in_days = 14
}

# --- shared plumbing -------------------------------------------------------

locals {
  # Every non-secret env var all three processes need. Process-specific
  # values (metrics port, consumer group) are added per task definition
  # below, on top of this list.
  common_environment = [
    { name = "ENV", value = var.environment },
    { name = "KAFKA_CLIENT_ID", value = "payments-${var.environment}" },
    { name = "KAFKA_SECURITY_PROTOCOL", value = "SASL_SSL" },
    { name = "KAFKA_SASL_MECHANISM", value = "SCRAM-SHA-256" }, # Redpanda Cloud's default SASL mechanism
  ]

  # {name, valueFrom} pairs for the ECS container definition's `secrets`
  # field — the execution role resolves each ARN into an env var of the
  # given name at container start (ADR 0009's secrets section). Every
  # process gets all six: the api needs DATABASE_URL/REDIS_URL/API_KEY but
  # not Kafka credentials directly (it only writes to the outbox table, per
  # CLAUDE.md invariant 6); the relay and worker need the Kafka credentials
  # but not API_KEY. Granting the full set to all three is simpler than
  # three different lists for a difference with no security consequence —
  # the task's own IAM role, not which env vars happen to be unused inside
  # the process, is what actually gates what a compromised container could
  # reach.
  app_secrets_env = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.app["database-url"].arn },
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.app["redis-url"].arn },
    { name = "KAFKA_BOOTSTRAP_SERVERS", valueFrom = aws_secretsmanager_secret.app["kafka-bootstrap-servers"].arn },
    { name = "KAFKA_SASL_USERNAME", valueFrom = aws_secretsmanager_secret.app["kafka-sasl-username"].arn },
    { name = "KAFKA_SASL_PASSWORD", valueFrom = aws_secretsmanager_secret.app["kafka-sasl-password"].arn },
    { name = "API_KEY", valueFrom = aws_secretsmanager_secret.app["api-key"].arn },
  ]

  log_options = {
    "awslogs-group"         = aws_cloudwatch_log_group.app.name
    "awslogs-region"        = data.aws_region.current.name
    "awslogs-stream-prefix" = "ecs"
  }
}

# --- api ---------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "payments-platform-${var.environment}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name  = "api"
    image = var.container_image
    # Dockerfile's CMD already runs uvicorn on :8000 — no override needed.
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.common_environment
    secrets      = local.app_secrets_env
    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true # see network.tf's file-level comment for why (no NAT Gateway)
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # Registering a NEW task definition (a deploy) does not, by itself, move
  # traffic — ECS only rolls a running service onto a new revision when the
  # service resource's task_definition changes too, which is exactly what
  # `aws ecs update-service --task-definition <new-revision>` in
  # .github/workflows/deploy.yml does. This lifecycle block stops
  # `terraform apply` from fighting that: without it, apply would try to
  # reset the service back to whatever revision is in THIS file's state,
  # undoing the CI/CD pipeline's most recent deploy.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.api_http]
}

resource "aws_appautoscaling_target" "api" {
  max_capacity       = 4
  min_capacity       = var.api_desired_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  # Only the api scales on its own policy — this is ADR 0009 Decision 3's
  # claim made concrete: a traffic spike grows api's task count without
  # touching relay's or worker's desired_count at all, because they're
  # entirely separate aws_ecs_service/aws_appautoscaling_target resources.
  name               = "cpu-target-tracking"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 60
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

# --- relay ---------------------------------------------------------------------

resource "aws_ecs_task_definition" "relay" {
  family                   = "payments-platform-${var.environment}-relay"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.relay_cpu
  memory                   = var.relay_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name         = "relay"
    image        = var.container_image
    command      = ["python", "-m", "app.events.relay"]         # same image as api, different command — ADR 0009 Decision 4
    portMappings = [{ containerPort = 9101, protocol = "tcp" }] # ADR 0007 Decision 3: the relay's own /metrics, not part of the api's FastAPI app
    environment  = local.common_environment
    secrets      = local.app_secrets_env
    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options
    }
  }])
}

resource "aws_ecs_service" "relay" {
  name            = "relay"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.relay.arn
  desired_count   = var.relay_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  # No load_balancer block — the relay has no reason to sit behind an ALB;
  # nothing calls it over HTTP.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

# --- worker (read-model consumer) --------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "payments-platform-${var.environment}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name         = "worker"
    image        = var.container_image
    command      = ["python", "-m", "app.events.consumer"]
    portMappings = [{ containerPort = 9102, protocol = "tcp" }] # settings.consumer_metrics_port — kafka_consumer_lag lives here, see docs/runbook.md
    environment  = local.common_environment
    secrets      = local.app_secrets_env
    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  # min=100%/max=200%: a rolling deploy starts the new task BEFORE stopping
  # the old one, so there's a brief window with two workers in the same
  # consumer group. That's safe by design, not by luck — ADR 0002 and
  # ADR 0006 already require every consumer to dedup on event_id, so Kafka
  # rebalancing partitions across two instances mid-deploy costs nothing
  # more than the rebalance itself. min=100% is what actually matters here:
  # it guarantees desired_count never dips to 0 mid-deploy, which is the
  # thing that would let kafka_consumer_lag actually climb (see
  # docs/runbook.md).
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}
