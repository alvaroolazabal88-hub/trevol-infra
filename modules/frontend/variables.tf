variable "project" {
  type        = string
  description = "Short project name"
}

variable "domain_name" {
  type        = string
  description = "Site domain, e.g. trevolcamaguey.com. Empty if not active yet."
  default     = ""
}

variable "acm_certificate_arn" {
  type        = string
  description = "ARN of the ACM certificate (us-east-1) for the domain. Empty if there is no domain yet."
  default     = ""
}

variable "api_domain_name" {
  type        = string
  description = "Invocable API Gateway domain, so CloudFront can use it as a second origin (/api/*)"
  default     = ""
}
