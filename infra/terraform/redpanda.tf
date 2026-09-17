# The managed Kafka layer ADR 0009 Decision 2 picks: Redpanda Cloud
# Serverless. This is the only non-AWS resource in this stack — the
# redpanda-data/redpanda provider talks to Redpanda Cloud's own control
# plane over its own API, not to AWS at all. Everything else in this
# directory (VPC, RDS, ElastiCache, ECS) is provisioned in this AWS account;
# the Kafka cluster is provisioned in Redpanda's account, reached from ECS
# over the public internet (network.tf's file-level comment covers why that
# needs no VPC peering or NAT path).
#
# Auth: REDPANDA_CLIENT_ID / REDPANDA_CLIENT_SECRET (or REDPANDA_ACCESS_TOKEN)
# environment variables, never a literal value here — the identical "never a
# credential in this repo or in state" rule ADR 0009's secrets section holds
# the app's own DATABASE_URL etc. to. `terraform validate` needs none of
# these (no API calls); `plan`/`apply` do. Provider version pinned in
# versions.tf, alongside hashicorp/aws.

provider "redpanda" {}

resource "redpanda_resource_group" "main" {
  name = "payments-platform-${var.environment}"
}

resource "redpanda_serverless_cluster" "main" {
  name              = "payments-platform-${var.environment}"
  resource_group_id = redpanda_resource_group.main.id
  serverless_region = var.redpanda_serverless_region

  tags = { Name = "payments-platform-${var.environment}" }
}

# Topics as code, not a human running `make topics` (Makefile) against prod
# by hand — the two are kept in exact sync (same names, same partition
# counts) so dev and prod can never silently drift apart on how a topic is
# keyed or how parallel its consumers can be.
locals {
  redpanda_topics = {
    # payments.payment.v1 and trading.order.v1 both carry per-aggregate
    # ordering keys (payment id; instrument id — see app/events/topics.py)
    # spread across 3 partitions for write parallelism. payments.dlq.v1 is
    # low-volume by design (see docs/runbook.md's DLQ section) and gains
    # nothing from more than 1.
    "payments.payment.v1" = 3
    "payments.dlq.v1"     = 1
    "trading.order.v1"    = 3
  }
}

resource "redpanda_topic" "app" {
  for_each = local.redpanda_topics

  name = each.key
  # dataplane_api.url, not the deprecated top-level cluster_api_url — same
  # value, current field name per the provider's own deprecation notice.
  cluster_api_url = redpanda_serverless_cluster.main.dataplane_api.url
  partition_count = each.value
  # No replication_factor override — Serverless manages replication itself
  # (ADR 0009 Decision 2: this is exactly the operational surface "managed"
  # is buying freedom from); self-managed KRaft locally
  # (docker-compose.yml) uses replication_factor=1 because there is only
  # one broker to replicate to, which is not a choice available or
  # meaningful on a multi-tenant serverless cluster.
}
