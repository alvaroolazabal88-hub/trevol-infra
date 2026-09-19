output "api_endpoint" {
  value = aws_apigatewayv2_api.orders.api_endpoint
}

output "api_domain_name" {
  # CloudFront necesita solo el host, sin "https://"
  value = replace(aws_apigatewayv2_api.orders.api_endpoint, "https://", "")
}

output "orders_table_name" {
  value = aws_dynamodb_table.orders.name
}

output "coupons_table_name" {
  value = aws_dynamodb_table.coupons.name
}

output "customers_table_name" {
  value = aws_dynamodb_table.customers.name
}
