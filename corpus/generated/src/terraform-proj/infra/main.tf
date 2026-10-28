terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "assets" {
  bucket = "shop-assets-${random_id.suffix.hex}"
  tags   = local.tags
}

module "api" {
  source       = "../modules/service"
  name         = "shop-api"
  image        = "ghcr.io/example/shop-api:1.4.0"
  replicas     = var.api_replicas
  environment  = var.environment
}

output "bucket" {
  value = aws_s3_bucket.assets.bucket
}
