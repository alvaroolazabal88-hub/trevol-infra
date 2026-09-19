# ============================================================================
# MODULO BUDGET — avisa por correo ANTES de que el gasto se salga del tope.
# Dos alarmas: una temprana (60% = $1.80) y una en el limite real ($3).
# Esto no bloquea el gasto, solo avisa -- AWS Budgets no puede "cortar" el
# gasto en tiempo real, solo notificar.
# ============================================================================

resource "aws_budgets_budget" "monthly_cap" {
  name         = "${var.project}-monthly-cap"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 60
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
