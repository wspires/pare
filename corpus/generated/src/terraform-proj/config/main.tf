variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod"
  }
}

variable "replicas" {
  type    = number
  default = 2
}

variable "ports" {
  type    = list(number)
  default = [80, 443, "8080"]
}

locals {
  name    = "shop-${var.environment}"
  domain  = var.environment == "prod" ? "shop.example.com" : "${var.environment}.shop.example.com"
  cpu     = var.replicas * 256
  tags    = merge(var.tags, { name = local.name })
  primary = element(var.ports, 0)
  subnets = [for s in var.subnet_ids : upper(s)]
  banner  = formatdate("YYYY-MM-DD", timestamp(), "UTC")
}

output "name" {
  value = local.name
}

output "domain" {
  value = local.domain
}

output "config" {
  value = {
    cpu     = local.cpu
    primary = local.primary
    subnets = local.subnets
    banner  = local.banner
    missing = local.region
  }
}

output "replicas" {
  value       = var.replicas
  description = "number of api replicas"
  sensitive   = false
}
