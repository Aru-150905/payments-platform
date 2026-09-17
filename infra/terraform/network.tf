# Deliberately no NAT Gateway. The usual shape (ECS tasks in private
# subnets, egress through a NAT Gateway) costs a fixed ~$0.045/hr per NAT
# Gateway PLUS per-GB data processing, whether or not a single task is
# running — a bad trade for a portfolio project whose traffic is "a demo
# script, occasionally." Redpanda Serverless (ADR 0009 Decision 2) is
# reachable over a public SASL_SSL endpoint, so Kafka egress doesn't need a
# NAT path either.
#
# Instead: ECS tasks run in the PUBLIC subnets with `assign_public_ip =
# true` (ecs.tf), reachable inbound only through the ALB's security group —
# nothing else is allowed to hit the task ENI directly (see
# security_groups.tf). RDS and ElastiCache sit in the PRIVATE subnets: they
# never initiate outbound internet traffic, so "private" here just means "no
# route to the internet gateway," not "behind a NAT" — they don't need one.
#
# The tradeoff being accepted: a task's public IP is one more thing an
# attacker could try to reach directly instead of through the ALB. The
# security group is what actually closes that off (ingress only from the
# ALB's SG on the app port), not subnet placement — worth revisiting with a
# NAT Gateway once real traffic justifies the ~$32+/mo.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "payments-platform-${var.environment}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "payments-platform-${var.environment}" }
}

resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "payments-platform-${var.environment}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + var.az_count)
  availability_zone = local.azs[count.index]

  tags = { Name = "payments-platform-${var.environment}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "payments-platform-${var.environment}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private route table stays route-less to the internet on purpose (see the
# file-level comment) — this association exists only so the subnet has an
# explicit table instead of silently falling back to the VPC's main one.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "payments-platform-${var.environment}-private" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
