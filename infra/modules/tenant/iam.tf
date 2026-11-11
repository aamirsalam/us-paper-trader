# Multi-tenant isolation inside one AWS account.
#
# Every role in this tenant carries the same permissions boundary. The boundary only
# allows actions on resources whose names start with var.name, so even a mistake in
# one of the role policies below can't reach the NSE tenant's table, secrets or logs.

locals {
  table_arn   = aws_dynamodb_table.ledger.arn
  log_arn     = "${aws_cloudwatch_log_group.this.arn}:*"
  repo_arn    = aws_ecr_repository.this.arn
  taskdef_arn = "arn:${local.partition}:ecs:${local.region}:${local.account}:task-definition/${var.name}"
  role_prefix = "arn:${local.partition}:iam::${local.account}:role/${var.name}-*"
}

data "aws_iam_policy_document" "boundary" {
  statement {
    sid       = "Ledger"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [local.table_arn]
  }
  statement {
    sid       = "Secrets"
    actions   = ["ssm:GetParameters", "ssm:GetParameter"]
    resources = [local.ssm_path_arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [local.log_arn]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource-level scoping
  }
  statement {
    sid = "EcrRepo"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
    ]
    resources = [local.repo_arn]
  }
  statement {
    sid       = "RunTask"
    actions   = ["ecs:RunTask"]
    resources = [local.taskdef_arn, "${local.taskdef_arn}:*"]
  }
  statement {
    sid       = "PassOwnRoles"
    actions   = ["iam:PassRole"]
    resources = [local.role_prefix]
  }
}

resource "aws_iam_policy" "boundary" {
  name        = "${var.name}-boundary"
  description = "Permissions boundary: ${var.name} roles can only touch ${var.name} resources"
  policy      = data.aws_iam_policy_document.boundary.json
}

# ---- ECS task execution role: pull image, write logs, read this tenant's SSM params ---------------
data "aws_iam_policy_document" "ecs_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "execution" {
  name                 = "${var.name}-execution"
  assume_role_policy   = data.aws_iam_policy_document.ecs_trust.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "execution" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [local.repo_arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [local.log_arn]
  }
  statement {
    actions   = ["ssm:GetParameters"]
    resources = [local.ssm_path_arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "pull-log-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---- Task role: what the trader code itself can do. Only its own ledger table. --------------------
resource "aws_iam_role" "task" {
  name                 = "${var.name}-task"
  assume_role_policy   = data.aws_iam_policy_document.ecs_trust.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "task" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [local.table_arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "ledger"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ---- EventBridge Scheduler role: start this tenant's task, nothing else ----------------------------
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name                 = "${var.name}-scheduler"
  assume_role_policy   = data.aws_iam_policy_document.scheduler_trust.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = [local.taskdef_arn, "${local.taskdef_arn}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "run-trader"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# ---- GitHub Actions deploy role (OIDC, no stored AWS keys): push images to this repo only ---------
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  oidc_arn      = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.github_oidc_provider_arn
  deploy_enable = var.github_repo != "" && local.oidc_arn != ""
}

data "aws_iam_policy_document" "github_trust" {
  count = local.deploy_enable ? 1 : 0
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  count                = local.deploy_enable ? 1 : 0
  name                 = "${var.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_trust[0].json
  permissions_boundary = aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
    ]
    resources = [local.repo_arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  count  = local.deploy_enable ? 1 : 0
  name   = "push-image"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy.json
}
