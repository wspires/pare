variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "environment" {
  type = string
}

variable "api_replicas" {
  type    = number
  default = 2
}

locals {
  tags = {
    project     = "shop"
    environment = var.environment
  }
}
