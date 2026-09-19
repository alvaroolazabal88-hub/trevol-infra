variable "project" {
  type = string
}

variable "lambda_zip_path" {
  type        = string
  description = "Ruta al .zip empaquetado del Lambda"
}

variable "lambda_source_hash" {
  type        = string
  description = "Hash del código fuente, para que Terraform sepa cuándo redesplegar"
}

# ---- Notificaciones (todas opcionales -- si se dejan vacías, el Lambda
# simplemente no manda esa notificación, el pedido igual se guarda bien) ----

variable "telegram_bot_token" {
  type        = string
  description = "Token del bot de Telegram (via @BotFather) para avisar pedidos nuevos"
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  type        = string
  description = "Chat id de Telegram donde llegan los avisos de pedido"
  default     = ""
  sensitive   = true
}

variable "twilio_account_sid" {
  type        = string
  description = "Account SID de Twilio, para mandar WhatsApp al cliente"
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
  description = "Número de WhatsApp habilitado en Twilio (formato +1XXXXXXXXXX)"
  default     = ""
  sensitive   = true
}
