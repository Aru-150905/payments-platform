resource "aws_db_subnet_group" "main" {
  name       = "payments-platform-${var.environment}"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "payments-platform-${var.environment}" }
}

resource "aws_db_instance" "postgres" {
  identifier     = "payments-platform-${var.environment}"
  engine         = "postgres"
  engine_version = "16"

  instance_class         = var.db_instance_class
  allocated_storage      = var.db_allocated_storage_gb
  storage_type           = "gp3"
  storage_encrypted      = true
  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  db_name  = "payments"
  username = "app"

  # RDS generates and stores the master password itself, in a Secrets
  # Manager secret it manages — the password never passes through a
  # Terraform variable, plan diff, or state file value, which is a strictly
  # better answer than the "we create an empty secret, a human fills it in"
  # pattern secrets.tf uses for the app's own credentials (RDS doesn't offer
  # that flow, this is the equivalent guarantee via a different mechanism).
  # The app's own DATABASE_URL secret (secrets.tf) still gets its password
  # segment copied in from this generated secret by hand during setup — see
  # infra/terraform/README.md.
  manage_master_user_password = true

  backup_retention_period = var.db_backup_retention_days
  skip_final_snapshot     = var.environment != "prod"
  deletion_protection     = var.environment == "prod"

  # Off — a portfolio deployment's Postgres minor-version drift is not worth
  # a surprise restart during a demo. Patch deliberately instead.
  auto_minor_version_upgrade = false

  tags = { Name = "payments-platform-${var.environment}" }
}
