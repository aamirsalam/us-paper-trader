# One schedule per phase, in New York time so daylight-saving changes need no edits.
# Uses Scheduler's universal ECS RunTask target so the phase is passed as a command override.

resource "aws_scheduler_schedule_group" "this" {
  name = var.name
}

resource "aws_scheduler_schedule" "phase" {
  for_each = var.schedules

  name                         = "${var.name}-${each.key}"
  group_name                   = aws_scheduler_schedule_group.this.name
  schedule_expression          = each.value
  schedule_expression_timezone = "America/New_York"
  state                        = var.schedules_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:${local.partition}:scheduler:::aws-sdk:ecs:runTask"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      Cluster         = aws_ecs_cluster.this.arn
      TaskDefinition  = aws_ecs_task_definition.trader.arn_without_revision
      LaunchType      = "FARGATE"
      PlatformVersion = "LATEST"
      Count           = 1
      NetworkConfiguration = {
        AwsvpcConfiguration = {
          Subnets        = aws_subnet.public[*].id
          SecurityGroups = [aws_security_group.task.id]
          AssignPublicIp = "ENABLED"
        }
      }
      Overrides = {
        ContainerOverrides = [{ Name = "trader", Command = [each.key] }]
      }
    })

    # Phases are idempotent, so a retry after e.g. a Fargate capacity hiccup is safe.
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 1800
    }
  }
}
