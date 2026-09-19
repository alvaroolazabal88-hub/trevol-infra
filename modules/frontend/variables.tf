variable "project" {
  type        = string
  description = "Nombre corto del proyecto"
}

variable "domain_name" {
  type        = string
  description = "Dominio del sitio, ej. trevolcamaguey.com. Vacío si todavía no está activo."
  default     = ""
}

variable "acm_certificate_arn" {
  type        = string
  description = "ARN del certificado ACM (us-east-1) para el dominio. Vacío si todavía no hay dominio."
  default     = ""
}

variable "api_domain_name" {
  type        = string
  description = "Dominio invocable del API Gateway, para que CloudFront lo use como segundo origin (/api/*)"
  default     = ""
}
