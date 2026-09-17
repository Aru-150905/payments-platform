resource "aws_elasticache_subnet_group" "main" {
  name       = "payments-platform-${var.environment}"
  subnet_ids = aws_subnet.private[*].id
}

# A single-node cluster, not a replication group — this project's Redis
# usage (ADR 0007 Decision 1: sliding-window rate-limit counters) is
# ephemeral, reconstructible state with a ~1-minute window. Losing it in a
# node failure means the rate limiter briefly resets to "everyone has a
# fresh budget," not data loss anything downstream depends on, so paying for
# a replica purely for Redis failover isn't justified at this scale.
resource "aws_elasticache_cluster" "redis" {
  cluster_id      = "payments-platform-${var.environment}"
  engine          = "redis"
  engine_version  = "7.1"
  node_type       = var.redis_node_type
  num_cache_nodes = 1
  port            = 6379

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  tags = { Name = "payments-platform-${var.environment}" }
}
