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
  excludes    = ["__pycache__", "__pycache__/*", "*.pyc"]
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

  worker_key              = local.worker_key
  telegram_webhook_secret = random_password.telegram_webhook_secret.result
  telegram_worker_ids     = var.telegram_worker_ids
  kitchen_lat             = var.kitchen_lat
  kitchen_lng             = var.kitchen_lng
  delivery_slots          = var.delivery_slots
  prep_minutes            = var.prep_minutes
  slot_lead_minutes       = var.slot_lead_minutes
  site_url                = var.domain_active ? "https://${var.domain_name}" : ""
}

# The single shared key for the worker panel (panel.html). Generated on its own,
# lowercase + digits so it is easy to type on a phone. Read it with:
#   terraform output -raw worker_key
# To choose your own, set worker_key in terraform.tfvars.
resource "random_password" "worker_key" {
  length  = 12
  special = false
  upper   = false
}

locals {
  worker_key = var.worker_key != "" ? var.worker_key : random_password.worker_key.result
}

# Secret Telegram must send back on every webhook call, so nobody else can
# post fake rider locations.
resource "random_password" "telegram_webhook_secret" {
  length  = 40
  special = false
}

# Tells Telegram where to send the rider's live location. Runs from YOUR
# machine (needs internet + curl, like the CloudFront invalidation below) and
# only again if the URL, the secret or the bot token changes. Note: a bot with a
# webhook set cannot also be polled with getUpdates.
resource "null_resource" "telegram_webhook" {
  count = nonsensitive(var.telegram_bot_token != "") ? 1 : 0

  triggers = {
    url    = "${module.backend_api.api_endpoint}/api/telegram/webhook"
    secret = sha256(random_password.telegram_webhook_secret.result)
    token  = sha256(var.telegram_bot_token)
  }

  provisioner "local-exec" {
    command = "curl -fsS -X POST \"https://api.telegram.org/bot$TG_TOKEN/setWebhook\" --data-urlencode \"url=$TG_URL\" --data-urlencode \"secret_token=$TG_SECRET\" --data-urlencode 'allowed_updates=[\"message\",\"edited_message\"]' --data-urlencode drop_pending_updates=true && echo"
    environment = {
      TG_TOKEN  = var.telegram_bot_token
      TG_URL    = "${module.backend_api.api_endpoint}/api/telegram/webhook"
      TG_SECRET = random_password.telegram_webhook_secret.result
    }
  }
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
    ".html"        = "text/html"
    ".css"         = "text/css"
    ".js"          = "application/javascript"
    ".json"        = "application/json"
    ".png"         = "image/png"
    ".jpg"         = "image/jpeg"
    ".jpeg"        = "image/jpeg"
    ".svg"         = "image/svg+xml"
    ".webp"        = "image/webp"
    ".ico"         = "image/x-icon"
    ".webmanifest" = "application/manifest+json"
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
