# Email when a trader run fails, so a missing morning report isn't the only sign.
# A run "fails" if the container exits non-zero (any mode errored) or the task never starts.

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email # AWS emails a confirmation link; alerts start after you click it
}

resource "aws_cloudwatch_event_rule" "task_failed" {
  name        = "${var.name}-task-failed"
  description = "A ${var.name} Fargate task exited with an error or failed to start"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.this.arn]
      lastStatus = ["STOPPED"]
      "$or" = [
        { containers = { exitCode = [{ anything-but = 0 }] } },
        { stopCode = ["TaskFailedToStart"] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "task_failed" {
  rule = aws_cloudwatch_event_rule.task_failed.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      command = "$.detail.overrides.containerOverrides[0].command[0]"
      code    = "$.detail.containers[0].exitCode"
      reason  = "$.detail.stoppedReason"
      task    = "$.detail.taskArn"
      time    = "$.time"
    }
    input_template = "\"${var.name}: the <command> run failed at <time> (exit code <code>). Reason: <reason>. Logs: CloudWatch log group ${local.log_group_name}, task <task>. Re-run with scripts/run-phase.sh <command>.\""
  }
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "EventBridgePublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.task_failed.arn]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}
