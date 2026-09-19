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
  description = "false hasta que llegue el correo de AWS confirmando el dominio. En false: se despliega todo sin dominio propio (CloudFront da su propia URL). En true: se conecta Route 53 + HTTPS con el dominio real."
  default     = false
}

variable "alert_email" {
  type        = string
  description = "Correo donde llegan los avisos de AWS Budgets"
}

variable "monthly_budget_usd" {
  type    = number
  default = 3
}

# ---- Notificaciones (opcionales) ----

variable "telegram_bot_token" {
  type        = string
  description = "Token del bot de Telegram (@BotFather) que avisa pedidos nuevos al negocio"
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  type        = string
  description = "Chat id de Telegram donde llegan los avisos"
  default     = ""
  sensitive   = true
}

variable "twilio_account_sid" {
  type        = string
  description = "Account SID de Twilio (para WhatsApp al cliente)"
  default     = ""
  sensitive   = true
}

variable "twilio_auth_token" {
  type        = string
  description = "Auth Token de Twilio"
  default     = ""
  sensitive   = true
}

variable "twilio_whatsapp_from" {
  type        = string
  description = "Número de WhatsApp habilitado en Twilio, formato +1XXXXXXXXXX"
  default     = ""
  sensitive   = true
}
