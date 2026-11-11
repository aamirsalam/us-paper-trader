output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "cluster_arn" {
  value = aws_ecs_cluster.this.arn
}

output "task_definition_family" {
  value = aws_ecs_task_definition.trader.family
}

output "subnet_ids" {
  value = aws_subnet.public[*].id
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}

output "ledger_table" {
  value = aws_dynamodb_table.ledger.name
}

output "ssm_parameters" {
  description = "Fill these with scripts/put-secrets.sh"
  value       = sort([for p in aws_ssm_parameter.secret : p.name])
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub"
  value       = try(aws_iam_role.deploy[0].arn, "")
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
}
