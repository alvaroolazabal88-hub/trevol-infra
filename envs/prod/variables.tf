variable "project" {
  type    = string
  default = "trevol"
}

variable "domain_name" {
  type    = string
  default = "trevolcamaguey.com"
}

variable "domain_active" {
  type        = bool
  description = "false until the AWS email confirming the domain arrives. At false: everything deploys without the custom domain (CloudFront gives its own URL). At true: connects Route 53 + HTTPS with the real domain."
  default     = false
}

variable "alert_email" {
  type        = string
  description = "Email address AWS Budgets sends alerts to"
}

variable "monthly_budget_usd" {
  type    = number
  default = 3
}

# ---- Notifications (optional) ----

variable "telegram_bot_token" {
  type        = string
  description = "Telegram bot token (@BotFather) that notifies the business of new orders"
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  type        = string
  description = "Telegram chat id where notifications arrive"
  default     = ""
  sensitive   = true
}

variable "twilio_account_sid" {
  type        = string
  description = "Twilio Account SID (for WhatsApp to the customer)"
  default     = ""
  sensitive   = true
}

variable "twilio_auth_token" {
  type        = string
  description = "Twilio Auth Token"
  default     = ""
  sensitive   = true
}

variable "twilio_whatsapp_from" {
  type        = string
  description = "WhatsApp-enabled number on Twilio, format +1XXXXXXXXXX"
  default     = ""
  sensitive   = true
}
