resource "aws_security_group" "alb" {
  name        = "payments-platform-${var.environment}-alb"
  description = "Public entry point. Only the api's ALB listens on the internet."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from anywhere (see infra/terraform/README.md for the HTTPS/ACM follow-up)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "payments-platform-${var.environment}-alb" }
}

# One security group for all three ECS services (api/relay/worker). They
# don't need different ingress rules from each other — only the api accepts
# any inbound at all (from the ALB, added below), and the relay/worker
# accept none. Splitting this into three SGs would be a rule set with two
# identical "no inbound" copies for no behavioural difference.
resource "aws_security_group" "ecs_tasks" {
  name        = "payments-platform-${var.environment}-ecs-tasks"
  description = "api/relay/worker Fargate tasks."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "ECR pull, Redpanda Cloud SASL_SSL, RDS, ElastiCache - all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "payments-platform-${var.environment}-ecs-tasks" }
}

resource "aws_security_group_rule" "ecs_tasks_ingress_from_alb" {
  # Only the api container listens; only the ALB may reach it. The relay
  # and worker get no ingress rule at all - a Fargate task with no rule
  # matching its ENI simply cannot be reached from anywhere.
  description              = "Only the ALB may reach the api container on 8000"
  type                     = "ingress"
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.ecs_tasks.id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_security_group" "rds" {
  name        = "payments-platform-${var.environment}-rds"
  description = "Postgres. Reachable only from the ECS tasks' security group."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "payments-platform-${var.environment}-rds" }
}

resource "aws_security_group" "redis" {
  name        = "payments-platform-${var.environment}-redis"
  description = "Redis. Reachable only from the ECS tasks' security group."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "payments-platform-${var.environment}-redis" }
}
