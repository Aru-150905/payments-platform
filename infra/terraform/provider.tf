provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "payments-platform"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
