# ============================================================================
# ENTRYPOINT prod — une frontend + backend-api + dns (opcional) + budget.
#
# Flujo normal de uso:
#   1) domain_active = false  -> terraform apply   (todo menos el dominio)
#   2) cuando llegue el correo de AWS confirmando trevolcamaguey.com:
#      domain_active = true  -> terraform apply de nuevo (conecta el dominio)
# ============================================================================

# ------------------------------------------------------- Empaquetar el Lambda
data "archive_file" "order_handler" {
  type        = "zip"
  source_dir  = "${path.module}/../../lambda/order_handler"
  output_path = "${path.module}/../../lambda/order_handler.zip"
}

# ------------------------------------------------------------------- Backend
module "backend_api" {
  source = "../../modules/backend-api"

  project            = var.project
  lambda_zip_path    = data.archive_file.order_handler.output_path
  lambda_source_hash = data.archive_file.order_handler.output_base64sha256

  telegram_bot_token   = var.telegram_bot_token
  telegram_chat_id     = var.telegram_chat_id
  twilio_account_sid   = var.twilio_account_sid
  twilio_auth_token    = var.twilio_auth_token
  twilio_whatsapp_from = var.twilio_whatsapp_from
}

# ----------------------------------------------------------------------- DNS
module "dns" {
  count  = var.domain_active ? 1 : 0
  source = "../../modules/dns"

  project     = var.project
  domain_name = var.domain_name
  create_zone = false # el dominio se registro DESDE Route 53: la zona ya existe sola.
}

# ------------------------------------------------------------------ Frontend
module "frontend" {
  source = "../../modules/frontend"

  project             = var.project
  domain_name         = var.domain_active ? var.domain_name : ""
  acm_certificate_arn = var.domain_active ? module.dns[0].certificate_arn : ""
  api_domain_name     = module.backend_api.api_domain_name
}

# --------------------------------------------------------- Registro del sitio
resource "aws_route53_record" "apex" {
  count   = var.domain_active ? 1 : 0
  zone_id = module.dns[0].zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = module.frontend.cloudfront_domain
    zone_id                = module.frontend.cloudfront_hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "www" {
  count   = var.domain_active ? 1 : 0
  zone_id = module.dns[0].zone_id
  name    = "www.${var.domain_name}"
  type    = "A"

  alias {
    name                   = module.frontend.cloudfront_domain
    zone_id                = module.frontend.cloudfront_hosted_zone_id
    evaluate_target_health = false
  }
}

# ------------------------------------------------ Subida de archivos del sitio
# Sube TODO lo que haya en /site al bucket automaticamente en cada apply.
# El etag (md5) hace que Terraform solo re-suba lo que cambio.
locals {
  content_types = {
    ".html" = "text/html"
    ".css"  = "text/css"
    ".js"   = "application/javascript"
    ".json" = "application/json"
    ".png"  = "image/png"
    ".jpg"  = "image/jpeg"
    ".jpeg" = "image/jpeg"
    ".svg"  = "image/svg+xml"
    ".webp" = "image/webp"
    ".ico"  = "image/x-icon"
  }
  site_files = fileset("${path.module}/../../site", "**/*")
}

resource "aws_s3_object" "site_files" {
  for_each = local.site_files

  bucket = module.frontend.bucket_name
  key    = each.value
  source = "${path.module}/../../site/${each.value}"
  etag   = filemd5("${path.module}/../../site/${each.value}")
  content_type = lookup(
    local.content_types,
    try(regex("\\.[^.]+$", each.value), ""),
    "application/octet-stream"
  )
}

# CloudFront cachea el HTML hasta 1h (default_ttl). Sin esto, cada cambio al
# sitio tardaria hasta 1h en verse. Se invalida solo cuando algun archivo
# realmente cambio (el trigger es el hash combinado de todos los etags).
resource "null_resource" "invalidate_cache" {
  triggers = {
    site_hash = md5(join("", [for f in aws_s3_object.site_files : f.etag]))
  }

  provisioner "local-exec" {
    command = "aws cloudfront create-invalidation --distribution-id ${module.frontend.cloudfront_distribution_id} --paths '/*'"
  }
}

# --------------------------------------------------------------------- Budget
module "budget" {
  source = "../../modules/budget"

  project           = var.project
  alert_email       = var.alert_email
  monthly_limit_usd = var.monthly_budget_usd
}
