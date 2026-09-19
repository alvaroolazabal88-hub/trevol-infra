output "zone_id" {
  value = local.zone_id
}

output "certificate_arn" {
  value = aws_acm_certificate_validation.site.certificate_arn
}
