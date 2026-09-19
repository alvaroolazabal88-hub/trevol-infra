output "bucket_name" {
  value = aws_s3_bucket.site.bucket
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.site.domain_name
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}

output "cloudfront_hosted_zone_id" {
  # Zone ID fijo de AWS para CUALQUIER distribucion CloudFront — hace falta
  # para el alias record de Route 53.
  value = "Z2FDTNDATAQYW2"
}
