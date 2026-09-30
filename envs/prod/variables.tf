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

# ---- Worker panel + delivery tracking (all optional) ----

variable "worker_key" {
  type        = string
  description = "Key the workers type into panel.html. Empty = Terraform generates one (terraform output -raw worker_key)"
  default     = ""
  sensitive   = true
}

variable "telegram_worker_ids" {
  type        = string
  description = "Comma-separated Telegram user ids allowed to share the rider's live location, e.g. \"123456789,987654321\". The chat in telegram_chat_id is always allowed. Any person who writes /start to the bot is told their id."
  default     = ""
}

variable "kitchen_lat" {
  type        = number
  description = "Kitchen latitude (delivery starting point). Can also be moved from the panel."
  default     = 21.3808
}

variable "kitchen_lng" {
  type    = number
  default = -77.9169
}

variable "delivery_slots" {
  type        = string
  description = "Delivery windows the customer can pick (JSON). Empty = breakfast 07:00-09:00 and snack 15:00-17:00. Example: [{\"id\":\"desayuno\",\"label\":\"Desayuno\",\"start\":\"07:00\",\"end\":\"09:00\"}]"
  default     = ""
}

variable "prep_minutes" {
  type        = number
  description = "Kitchen prep time for 'as soon as possible' estimates"
  default     = 25
}

variable "slot_lead_minutes" {
  type        = number
  description = "A delivery window stops accepting orders this many minutes before it ends"
  default     = 30
}
