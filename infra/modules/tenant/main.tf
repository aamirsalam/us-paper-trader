terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  region    = data.aws_region.current.region
  partition = data.aws_partition.current.partition

  ssm_prefix     = "/${var.name}"
  ssm_path_arn   = "arn:${local.partition}:ssm:${local.region}:${local.account}:parameter${local.ssm_prefix}/*"
  log_group_name = "/ecs/${var.name}"

  # name of env var -> SSM parameter (relative to the tenant prefix)
  secret_params = merge(
    merge([for m in var.modes : {
      "ALPACA_KEY_ID_${upper(m)}" = "alpaca/${m}/key_id"
      "ALPACA_SECRET_${upper(m)}" = "alpaca/${m}/secret"
    }]...),
    {
      SMTP_USER     = "smtp/user"
      SMTP_PASSWORD = "smtp/password"
      REPORT_TO     = "report/to"
    }
  )

  container_env = merge(
    {
      LEDGER_BACKEND       = "dynamodb"
      LEDGER_TABLE         = aws_dynamodb_table.ledger.name
      TRADER_MODES         = join(",", var.modes)
      STARTING_CAPITAL_USD = "1000"
      SMTP_HOST            = "smtp.gmail.com"
      SMTP_PORT            = "587"
      AWS_REGION           = local.region
    },
    var.app_env
  )
}

# ---- storage ---------------------------------------------------------------------------------------
resource "aws_dynamodb_table" "ledger" {
  name                        = "${var.name}-ledger"
  billing_mode                = "PROVISIONED" # 2 RCU/WCU sits inside DynamoDB's always-free 25/25
  read_capacity               = 2
  write_capacity              = 2
  hash_key                    = "mode"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "mode"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true # the table is kilobytes, so this costs effectively nothing
  }
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.secret_params

  name  = "${local.ssm_prefix}/${each.value}"
  type  = "SecureString" # standard tier + AWS-managed key: free
  value = "CHANGE_ME"

  lifecycle {
    ignore_changes = [value] # real values are set with scripts/put-secrets.sh, never in Terraform state
  }
}

# ---- container plumbing ----------------------------------------------------------------------------
resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "this" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "disabled" # Container Insights is billed; plain logs are enough here
  }
}

resource "aws_ecs_task_definition" "trader" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64" # Graviton: ~20% cheaper than x86 on Fargate
  }

  container_definitions = jsonencode([{
    name                   = "trader"
    image                  = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
    essential              = true
    command                = ["postclose"]
    readonlyRootFilesystem = true
    environment            = [for k, v in local.container_env : { name = k, value = v }]
    secrets                = [for k, p in aws_ssm_parameter.secret : { name = k, valueFrom = p.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.this.name
        awslogs-region        = local.region
        awslogs-stream-prefix = "trader"
      }
    }
  }])
}
