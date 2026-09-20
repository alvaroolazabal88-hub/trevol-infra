# ============================================================================
# BOOTSTRAP — applied ONCE, with local state, before everything else. Creates
# the S3 bucket that holds the real project's terraform.tfstate, and the
# DynamoDB table that keeps two "terraform apply" runs from racing each other.
#
# Usage:
#   cd bootstrap
#   terraform init
#   terraform apply
#
# After this, this folder is never touched again unless the whole project is
# being torn down from scratch.
# ============================================================================

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # Local state on purpose: this is the one module without a remote backend,
  # because it is the one that creates the remote backend.
}

provider "aws" {
  region = "us-east-1"
}

variable "project" {
  description = "Short project name, used as the prefix for resource names"
  type        = string
  default     = "trevol"
}

resource "aws_s3_bucket" "tf_state" {
  bucket = "${var.project}-terraform-state"

  # Guardrail: "terraform destroy" cannot delete this bucket by accident.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project = var.project
    Purpose = "terraform-state"
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lock table. Falls inside DynamoDB's Always Free tier (25 GB / 25 WCU-RCU
# provisioned) — at the usage of one developer applying Terraform now and
# then, this never generates a charge.
resource "aws_dynamodb_table" "tf_lock" {
  name         = "${var.project}-terraform-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = {
    Project = var.project
    Purpose = "terraform-lock"
  }
}

output "state_bucket" {
  value = aws_s3_bucket.tf_state.bucket
}

output "lock_table" {
  value = aws_dynamodb_table.tf_lock.name
}
