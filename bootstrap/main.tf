# ============================================================================
# BOOTSTRAP — se aplica UNA SOLA VEZ, con estado local, antes que todo lo demás.
# Crea el bucket S3 donde va a vivir el terraform.tfstate del proyecto real,
# y la tabla DynamoDB que evita que dos "terraform apply" corran a la vez.
#
# Uso:
#   cd bootstrap
#   terraform init
#   terraform apply
#
# Después de esto, jamás se vuelve a tocar esta carpeta salvo que se quiera
# destruir todo el proyecto desde cero.
# ============================================================================

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # Estado local a propósito: este bootstrap es el único módulo sin backend
  # remoto, porque es el que CREA el backend remoto.
}

provider "aws" {
  region = "us-east-1"
}

variable "project" {
  description = "Nombre corto del proyecto, usado como prefijo de recursos"
  type        = string
  default     = "trevol"
}

resource "aws_s3_bucket" "tf_state" {
  bucket = "${var.project}-terraform-state"

  # Protección: "terraform destroy" no puede borrar este bucket por accidente.
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

# Tabla de lock. Cae dentro del "Always Free" de DynamoDB (25 GB / 25 WCU-RCU
# provisionados) — con el uso de un solo desarrollador aplicando Terraform de
# vez en cuando, esto no genera cargo.
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
