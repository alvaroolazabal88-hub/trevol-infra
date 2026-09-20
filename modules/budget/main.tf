# ============================================================================
# BUDGET MODULE — emails a warning BEFORE spend goes over the cap.
# Two alarms: an early one (60% = $1.80) and one at the real limit ($3).
# This does not block spend, it only warns -- AWS Budgets cannot "cut off"
# spend in real time, only notify.
# ============================================================================

resource "aws_budgets_budget" "monthly_cap" {
  name         = "${var.project}-monthly-cap"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 60
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
