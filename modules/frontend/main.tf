# ============================================================================
# MODULO FRONTEND — sitio estatico en S3, servido por CloudFront.
# Si se pasa api_domain_name, CloudFront tambien enruta /api/* al backend,
# asi el sitio y la API viven bajo el MISMO dominio (sin problemas de CORS,
# y el cliente nunca ve la URL fea del API Gateway).
# ============================================================================

locals {
  has_domain = var.domain_name != "" && var.acm_certificate_arn != ""
  has_api    = var.api_domain_name != ""
  bucket_origin_id = "s3-site"
  api_origin_id    = "api-backend"
}

# ---------------------------------------------------------------- Bucket S3
resource "aws_s3_bucket" "site" {
  bucket = "${var.project}-site-${data.aws_caller_identity.current.account_id}"

  tags = { Project = var.project }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Nota: NO se usa aws_s3_bucket_website_configuration a proposito. Con OAC
# (Origin Access Control), CloudFront habla con el endpoint REST de S3, no
# con el endpoint de "static website hosting" -- ese endpoint exige el
# bucket publico, que es justo lo que evitamos al usar OAC. El index/404 se
# resuelve del lado de CloudFront (default_root_object + custom_error_response).

# --------------------------------------------------- CloudFront <-> S3 (OAC)
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.project}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.site.arn
        }
      }
    }]
  })
}

# -------------------------------------------------------- Distribucion CDN
resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # Solo NA + Europa: la mas barata, suficiente para Cuba/US.
  aliases             = local.has_domain ? [var.domain_name, "www.${var.domain_name}"] : []

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = local.bucket_origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  dynamic "origin" {
    for_each = local.has_api ? [1] : []
    content {
      domain_name = var.api_domain_name
      origin_id   = local.api_origin_id
      custom_origin_config {
        http_port              = 80
        https_port              = 443
        origin_protocol_policy  = "https-only"
        origin_ssl_protocols    = ["TLSv1.2"]
      }
    }
  }

  default_cache_behavior {
    allowed_methods       = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = local.bucket_origin_id
    viewer_protocol_policy = "redirect-to-https"
    compress                = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.has_api ? [1] : []
    content {
      path_pattern           = "/api/*"
      allowed_methods         = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      target_origin_id         = local.api_origin_id
      viewer_protocol_policy   = "redirect-to-https"
      compress                  = true
      cache_policy_id           = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    }
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = local.has_domain ? false : true
    acm_certificate_arn            = local.has_domain ? var.acm_certificate_arn : null
    ssl_support_method             = local.has_domain ? "sni-only" : null
    minimum_protocol_version       = local.has_domain ? "TLSv1.2_2021" : null
  }

  custom_error_response {
    error_code         = 404
    response_code      = 404
    response_page_path = "/404.html"
  }

  tags = { Project = var.project }
}

# Politicas administradas de AWS, reusadas para no reinventar cache/forwarding.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewerExceptHostHeader"
}
