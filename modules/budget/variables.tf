variable "project" {
  type = string
}

variable "alert_email" {
  type        = string
  description = "Correo donde llegan los avisos de gasto"
}

variable "monthly_limit_usd" {
  type    = number
  default = 3
}
