# ============================================================================
# DNS MODULE — Route 53 zone + ACM certificate (HTTPS) for the domain.
#
# IMPORTANT: when you register a domain THROUGH Route 53 (which is what
# happened here), AWS automatically creates a public hosted zone for that
# domain and wires up the nameservers on its own. That's why create_zone
# defaults to false: use the zone that ALREADY exists, instead of creating a
# second one and ending up with two hosted zones fighting over the same
# domain.
# ============================================================================

data "aws_route53_zone" "existing" {
  count = var.create_zone ? 0 : 1
  name  = var.domain_name
}

resource "aws_route53_zone" "new" {
  count = var.create_zone ? 1 : 0
  name  = var.domain_name
  tags  = { Project = var.project }
}

locals {
  zone_id = var.create_zone ? aws_route53_zone.new[0].zone_id : data.aws_route53_zone.existing[0].zone_id
}

# CloudFront requires the certificate to be in us-east-1 (no matter which
# region everything else runs in). This project's default provider is
# already us-east-1, so no provider alias is needed.
resource "aws_acm_certificate" "site" {
  domain_name               = var.domain_name
  subject_alternative_names = ["www.${var.domain_name}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Project = var.project }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.site.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }

  zone_id         = local.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "site" {
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}
