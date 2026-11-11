resource "aws_ce_cost_allocation_tag" "project" {
  count   = var.activate_cost_tag ? 1 : 0
  tag_key = "project"
  status  = "Active"
}

resource "aws_budgets_budget" "tenant" {
  count        = var.create_budget && var.alert_email != "" ? 1 : 0
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Filters on this tenant's tag. Until the tag is activated it reports 0, so the
  # account-wide alarm on the NSE side still covers you in the meantime.
  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:project$%s", var.name)]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
