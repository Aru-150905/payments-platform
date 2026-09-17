# Fronts the api service only. The relay and worker have no HTTP surface a
# load balancer would ever route to (their /metrics ports are for scraping
# from inside the VPC, not public traffic) — see ADR 0009 Decision 3.

resource "aws_lb" "api" {
  name               = "pp-${var.environment}-api"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "payments-platform-${var.environment}-api" }
}

resource "aws_lb_target_group" "api" {
  name        = "pp-${var.environment}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # required for awsvpc-networked Fargate tasks — there's no EC2 instance ID to register instead

  health_check {
    # /health/ready checks Postgres only, deliberately not Kafka (see
    # app/main.py's docstring) — this is exactly the check a load balancer
    # should gate routing on: "can this task serve reads and accept writes
    # into the outbox," not "is every downstream dependency perfect."
    path                = "/health/ready"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }

  tags = { Name = "payments-platform-${var.environment}-api" }
}

resource "aws_lb_listener" "api_http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  # Plain HTTP — this milestone has no ACM certificate or registered domain
  # to terminate TLS with (see infra/terraform/README.md's follow-ups). Not
  # appropriate for real payment traffic; fine for a reviewable dry-run
  # deployment of a portfolio project with no real cardholder or bank data
  # flowing through it.
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
