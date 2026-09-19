# ============================================================================
# MODULO DNS — zona de Route 53 + certificado ACM (HTTPS) para el dominio.
#
# IMPORTANTE: cuando registras un dominio DESDE Route 53 (que es lo que
# hiciste), AWS crea automaticamente una hosted zone publica para ese
# dominio y ya conecta los nameservers solo. Por eso create_zone=false por
# defecto: usamos la zona que YA existe, en vez de crear una segunda y
# terminar con dos hosted zones peleando por el mismo dominio.
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

# ---------------------------------------------------------------- Certificado
# CloudFront exige que el certificado este en us-east-1 (sin importar en que
# region corra el resto). El provider por defecto de este proyecto ya es
# us-east-1, asi que no hace falta alias de provider.
resource "aws_acm_certificate" "site" {
  domain_name               = var.domain_name
  subject_alternative_names = ["www.${var.domain_name}"]
  validation_method          = "DNS"

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

  zone_id = local.zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.value]
  ttl     = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "site" {
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}
