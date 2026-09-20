variable "project" {
  type = string
}

variable "domain_name" {
  type        = string
  description = "Domain name, e.g. trevolcamaguey.com"
}

variable "create_zone" {
  type        = bool
  description = "true the first time (creates the hosted zone). false on later applies if it already exists."
  default     = true
}
