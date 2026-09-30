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

variable "webhook_public_url" {
  type        = string
  description = "Exact public URL configured in Twilio to receive incoming messages (used to validate the signature)"
  default     = ""
}

variable "admin_token" {
  type        = string
  description = "Secret token for admin actions (e.g. marking a 'no-show')"
  default     = ""
  sensitive   = true
}

# ---- Worker panel + live delivery tracking ----

variable "worker_key" {
  type        = string
  description = "Single shared key the workers type into the panel (panel.html). Sent as the X-Worker-Key header."
  default     = ""
  sensitive   = true
}

variable "telegram_webhook_secret" {
  type        = string
  description = "Secret Telegram sends back on every webhook call (X-Telegram-Bot-Api-Secret-Token), so only Telegram can post live locations"
  default     = ""
  sensitive   = true
}

variable "telegram_worker_ids" {
  type        = string
  description = "Comma-separated Telegram user ids allowed to share the rider's live location (the chat in telegram_chat_id is always allowed). The bot replies /start with the id of whoever writes to it."
  default     = ""
}

variable "kitchen_lat" {
  type        = number
  description = "Starting point of every delivery (the kitchen). The panel can move it later. Default: Parque Agramonte, Camagüey"
  default     = 21.3808
}

variable "kitchen_lng" {
  type    = number
  default = -77.9169
}

variable "delivery_slots" {
  type        = string
  description = "JSON list of delivery windows the customer can pick, e.g. [{\"id\":\"desayuno\",\"label\":\"Desayuno\",\"start\":\"07:00\",\"end\":\"09:00\"}]. Empty = breakfast 7-9 and snack 15-17. 'Lo antes posible' is always offered."
  default     = ""
}

variable "prep_minutes" {
  type        = number
  description = "Kitchen prep time used for 'as soon as possible' estimates"
  default     = 25
}

variable "slot_lead_minutes" {
  type        = number
  description = "A window closes for orders this many minutes before it ends"
  default     = 30
}

variable "site_url" {
  type        = string
  description = "Public URL of the site (used for the panel link in Telegram alerts). Empty = no link"
  default     = ""
}
