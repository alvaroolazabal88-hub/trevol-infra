variable "project" {
  type = string
}

variable "domain_name" {
  type        = string
  description = "trevolcamaguey.com"
}

variable "create_zone" {
  type        = bool
  description = "true la primera vez (crea la hosted zone). false en aplicaciones posteriores si ya existe."
  default     = true
}
