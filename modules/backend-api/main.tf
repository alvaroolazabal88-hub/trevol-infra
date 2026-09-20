# ============================================================================
# BACKEND-API MODULE — DynamoDB (orders) + Lambda + API Gateway HTTP API.
#
# All pay-per-use. At the expected volume (dozens of orders a day, not
# thousands), this stays in cents of a dollar per month, well under the
# $3 cap.
# ============================================================================

# On-demand: no capacity to guess at, and at this volume it falls inside or
# very close to the "always free" tier (25 GB of storage).
resource "aws_dynamodb_table" "orders" {
  name         = "${var.project}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false # Keeps the cost at $0. Can be turned on later if needed.
  }

  tags = { Project = var.project }
}

# Discount coupons -- code is what the customer types in.
resource "aws_dynamodb_table" "coupons" {
  name         = "${var.project}-coupons"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "code"

  attribute {
    name = "code"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = { Project = var.project }
}

# Customers -- fills itself in with each order (name, alias, how much they've
# spent). Used later to send personalized WhatsApp promotions.
resource "aws_dynamodb_table" "customers" {
  name         = "${var.project}-customers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "phone"

  attribute {
    name = "phone"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = { Project = var.project }
}

resource "aws_iam_role" "lambda_exec" {
  name = "${var.project}-order-handler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_dynamodb" {
  name = "${var.project}-order-handler-dynamodb"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.orders.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.coupons.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.customers.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "order_handler" {
  function_name    = "${var.project}-order-handler"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  filename         = var.lambda_zip_path
  source_code_hash = var.lambda_source_hash
  timeout          = 10
  memory_size      = 128 # The minimum. More memory = faster but also pricier; 128MB is plenty here.

  environment {
    variables = {
      ORDERS_TABLE         = aws_dynamodb_table.orders.name
      COUPONS_TABLE        = aws_dynamodb_table.coupons.name
      CUSTOMERS_TABLE      = aws_dynamodb_table.customers.name
      TELEGRAM_BOT_TOKEN   = var.telegram_bot_token
      TELEGRAM_CHAT_ID     = var.telegram_chat_id
      TWILIO_ACCOUNT_SID   = var.twilio_account_sid
      TWILIO_AUTH_TOKEN    = var.twilio_auth_token
      TWILIO_WHATSAPP_FROM = var.twilio_whatsapp_from
    }
  }

  tags = { Project = var.project }
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.order_handler.function_name}"
  retention_in_days = 14 # Keeps logs from growing forever and generating storage cost.
}

# HTTP API (not REST API): cheaper -- $1.00 per million calls against $3.50
# for the classic REST API -- and has more than enough features for this.
resource "aws_apigatewayv2_api" "orders" {
  name          = "${var.project}-orders-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["content-type"]
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.orders.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.order_handler.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_order" {
  api_id    = aws_apigatewayv2_api.orders.id
  route_key = "POST /api/orders"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "validate_coupon" {
  api_id    = aws_apigatewayv2_api.orders.id
  route_key = "POST /api/coupons/validate"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.orders.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId"
      status    = "$context.status"
      path      = "$context.path"
    })
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.project}-orders-api"
  retention_in_days = 14
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.order_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.orders.execution_arn}/*/*"
}
