resource "aws_ecr_repository" "app" {
  name                 = "payments-platform"
  image_tag_mutability = "IMMUTABLE" # a git-sha tag is never reused, so overwriting one would only ever hide a mistake

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "payments-platform-${var.environment}" }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days — build cache layers and abandoned pushes, never a deployed tag."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the last 20 tagged images — enough rollback history (see docs/runbook.md) without unbounded storage growth."
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 20
        }
        action = { type = "expire" }
      }
    ]
  })
}
