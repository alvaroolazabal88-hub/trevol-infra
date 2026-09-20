# ============================================================================
# ENTRYPOINT prod — composes frontend + backend-api + dns (optional) + budget.
#
# Normal flow:
#   1) domain_active = false  -> terraform apply   (everything but the domain)
#   2) once the AWS email confirming trevolcamaguey.com arrives:
#      domain_active = true  -> terraform apply again (connects the domain)
# ============================================================================

data "archive_file" "order_handler" {
  type        = "zip"
  source_dir  = "${path.module}/../../lambda/order_handler"
  output_path = "${path.module}/../../lambda/order_handler.zip"
}

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
  webhook_public_url   = "https://${var.domain_name}/api/whatsapp/webhook"
  admin_token          = random_password.admin_token.result
}

# Token for admin actions (e.g. marking a "no-show"). Generated on its own --
# no need to invent one. Read it with:
#   terraform output -raw admin_token
resource "random_password" "admin_token" {
  length  = 32
  special = false
}

module "dns" {
  count  = var.domain_active ? 1 : 0
  source = "../../modules/dns"

  project     = var.project
  domain_name = var.domain_name
  create_zone = false # the domain was registered THROUGH Route 53: the zone already exists on its own.
}

module "frontend" {
  source = "../../modules/frontend"

  project             = var.project
  domain_name         = var.domain_active ? var.domain_name : ""
  acm_certificate_arn = var.domain_active ? module.dns[0].certificate_arn : ""
  api_domain_name     = module.backend_api.api_domain_name
}

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

# Uploads everything under /site to the bucket on every apply. The etag (md5)
# is what makes Terraform re-upload only what actually changed.
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

# CloudFront caches HTML for up to 1h (default_ttl). Without this, a site
# change could take up to an hour to show. It invalidates only when a file
# actually changed (the trigger is the combined hash of every etag).
resource "null_resource" "invalidate_cache" {
  triggers = {
    site_hash = md5(join("", [for f in aws_s3_object.site_files : f.etag]))
  }

  provisioner "local-exec" {
    command = "aws cloudfront create-invalidation --distribution-id ${module.frontend.cloudfront_distribution_id} --paths '/*'"
  }
}

module "budget" {
  source = "../../modules/budget"

  project           = var.project
  alert_email       = var.alert_email
  monthly_limit_usd = var.monthly_budget_usd
}
