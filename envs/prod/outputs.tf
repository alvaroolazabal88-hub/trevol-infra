output "site_url" {
  value = var.domain_active ? "https://${var.domain_name}" : "https://${module.frontend.cloudfront_domain}"
}

output "cloudfront_domain" {
  value = module.frontend.cloudfront_domain
}

output "s3_bucket" {
  value = module.frontend.bucket_name
}

output "api_endpoint" {
  value = module.backend_api.api_endpoint
}

output "orders_table" {
  value = module.backend_api.orders_table_name
}

output "coupons_table" {
  value = module.backend_api.coupons_table_name
}

output "customers_table" {
  value = module.backend_api.customers_table_name
}

output "admin_token" {
  value     = random_password.admin_token.result
  sensitive = true
}

output "next_step" {
  value = var.domain_active ? "Dominio conectado. El sitio ya responde en https://${var.domain_name}" : "Sitio arriba en la URL de CloudFront. Cuando llegue el correo de AWS confirmando el dominio, pon domain_active=true y vuelve a aplicar."
}
