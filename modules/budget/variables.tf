variable "project" {
  type = string
}

variable "alert_email" {
  type        = string
  description = "Email address that receives spend alerts"
}

variable "monthly_limit_usd" {
  type    = number
  default = 3
}
