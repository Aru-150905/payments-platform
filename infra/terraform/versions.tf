terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local backend by default — state lives in this directory, gitignored
  # (see infra/terraform/.gitignore). That is fine for a solo portfolio
  # project's dry-run/plan workflow and deliberately does NOT provision an
  # S3 bucket + DynamoDB lock table, which this milestone's "don't provision
  # anything that costs money" rule would otherwise forbid doing blind.
  #
  # Before a real `apply`, switch to a remote backend so state survives a
  # wiped laptop and two people can't apply concurrently:
  #
  #   terraform {
  #     backend "s3" {
  #       bucket         = "<your-state-bucket>"
  #       key            = "payments-platform/terraform.tfstate"
  #       region         = "<your-region>"
  #       dynamodb_table = "<your-lock-table>"
  #       encrypt        = true
  #     }
  #   }
  #
  # That bucket/table is itself a one-time `apply` a human runs deliberately,
  # not something this milestone creates on the reviewer's behalf.
}
