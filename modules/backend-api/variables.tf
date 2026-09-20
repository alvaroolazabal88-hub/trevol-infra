variable "project" {
  type = string
}

variable "lambda_zip_path" {
  type        = string
  description = "Path to the packaged Lambda .zip"
}

variable "lambda_source_hash" {
  type        = string
  description = "Hash of the source code, so Terraform knows when to redeploy"
}

# ---- Notifications (all optional -- if left empty, the Lambda simply skips
# that notification; the order is still saved either way) ----

variable "telegram_bot_token" {
  type        = string
  description = "Telegram bot token (via @BotFather) to notify new orders"
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  type        = string
  description = "Telegram chat id where order alerts arrive"
  default     = ""
  sensitive   = true
}

variable "twilio_account_sid" {
  type        = string
  description = "Twilio Account SID, to send WhatsApp to the customer"
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
  description = "WhatsApp-enabled number on Twilio (format +1XXXXXXXXXX)"
  default     = ""
  sensitive   = true
}
