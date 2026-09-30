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

  attribute {
    name = "phone"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  attribute {
    name = "delivery_day"
    type = "S"
  }

  # The worker panel asks "what's on for this day?" -- this index answers it
  # with one Query instead of scanning the whole table. Orders created before
  # this index existed have no delivery_day and simply don't show up in it.
  global_secondary_index {
    name            = "day-index"
    hash_key        = "delivery_day"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  # To quickly find "the pending-confirmation order for this phone number"
  # when the WhatsApp reply arrives -- without this it would mean scanning
  # the whole table on every message.
  global_secondary_index {
    name            = "phone-index"
    hash_key        = "phone"
    range_key       = "created_at"
    projection_type = "ALL"
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

# Small key/value table: the kitchen point, the rider's last position, the trip
# in progress, and addresses whose pin a worker already confirmed ("a#...").
# A handful of items -- effectively free.
resource "aws_dynamodb_table" "geo" {
  name         = "${var.project}-geo"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
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
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.orders.arn, "${aws_dynamodb_table.orders.arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.coupons.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.customers.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.geo.arn
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
  memory_size      = 256 # The address catalog (camaguey.json) is loaded into memory; 128MB gets tight. Still cents/month.

  environment {
    variables = {
      ORDERS_TABLE         = aws_dynamodb_table.orders.name
      COUPONS_TABLE        = aws_dynamodb_table.coupons.name
      CUSTOMERS_TABLE      = aws_dynamodb_table.customers.name
      GEO_TABLE            = aws_dynamodb_table.geo.name
      TELEGRAM_BOT_TOKEN   = var.telegram_bot_token
      TELEGRAM_CHAT_ID     = var.telegram_chat_id
      TWILIO_ACCOUNT_SID   = var.twilio_account_sid
      TWILIO_AUTH_TOKEN    = var.twilio_auth_token
      TWILIO_WHATSAPP_FROM = var.twilio_whatsapp_from
      WEBHOOK_PUBLIC_URL   = var.webhook_public_url
      ADMIN_TOKEN          = var.admin_token

      WORKER_KEY              = var.worker_key
      TELEGRAM_WEBHOOK_SECRET = var.telegram_webhook_secret
      TELEGRAM_WORKER_IDS     = var.telegram_worker_ids
      KITCHEN_LAT             = tostring(var.kitchen_lat)
      KITCHEN_LNG             = tostring(var.kitchen_lng)
      DELIVERY_SLOTS          = var.delivery_slots
      PREP_MIN                = tostring(var.prep_minutes)
      SLOT_LEAD_MIN           = tostring(var.slot_lead_minutes)
      SITE_URL                = var.site_url
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
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type", "x-worker-key"]
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

resource "aws_apigatewayv2_route" "whatsapp_webhook" {
  api_id    = aws_apigatewayv2_api.orders.id
  route_key = "POST /api/whatsapp/webhook"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "mark_no_show" {
  api_id    = aws_apigatewayv2_api.orders.id
  route_key = "POST /api/orders/{order_id}/no-show"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# Routes added for tracking, the worker panel and the Telegram location webhook.
# (The four above keep their original resource names so nothing gets recreated.)
locals {
  extra_routes = toset([
    "GET /api/config",
    "POST /api/geocode",
    "GET /api/track/{order_id}",
    "POST /api/telegram/webhook",
    "GET /api/panel/orders",
    "POST /api/panel/orders/{order_id}",
    "POST /api/panel/trip",
    "POST /api/panel/rider",
    "POST /api/panel/kitchen",
  ])
}

resource "aws_apigatewayv2_route" "extra" {
  for_each  = local.extra_routes
  api_id    = aws_apigatewayv2_api.orders.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.orders.id
  name        = "$default"
  auto_deploy = true

  # Cheap protection against floods (and against a runaway bill): API Gateway
  # rejects the excess with 429 before the Lambda is ever invoked.
  default_route_settings {
    throttling_burst_limit = 60
    throttling_rate_limit  = 30
  }

  route_settings {
    route_key              = "POST /api/orders"
    throttling_burst_limit = 10
    throttling_rate_limit  = 3
  }

  route_settings {
    route_key              = "POST /api/geocode"
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }

  depends_on = [
    aws_apigatewayv2_route.post_order,
    aws_apigatewayv2_route.extra,
  ]

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
