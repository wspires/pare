terraform {
  required_version = ">= 1.6"
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "services" {
  type = map(object({
    image    = string
    replicas = number
    port     = number
    cpu      = optional(number, 256)
    memory   = optional(number, 512)
  }))
  default = {
    api = { image = "ghcr.io/example/shop-api:1.4.0", replicas = 3, port = 8080 }
    web = { image = "ghcr.io/example/shop-web:0.3.1", replicas = 2, port = 3000, cpu = 128 }
    worker = { image = "ghcr.io/example/shop-worker:1.4.0", replicas = 1, port = 9000, memory = 2048 }
    search = { image = "ghcr.io/example/shop-search:2.0.0-rc1", replicas = 2, port = 9200, cpu = 1024, memory = 4096 }
    cron = { image = "ghcr.io/example/shop-cron:1.4.0", replicas = 1, port = 0 }
    mailer = { image = "ghcr.io/example/shop-mailer:0.9.7", replicas = 1, port = 2525 }
  }
}

variable "regions" {
  type    = list(string)
  default = ["eu-central-1", "us-east-1"]
}

locals {
  name_prefix = "shop-${var.environment}"
  deployments = merge([
    for region in var.regions : {
      for name, svc in var.services : "${region}/${name}" => merge(svc, { region = region, name = name })
    }
  ]...)
  total_cpu    = sum([for d in local.deployments : d.cpu * d.replicas])
  total_memory = sum([for d in local.deployments : d.memory * d.replicas])
}

resource "terraform_data" "network" {
  for_each = toset(var.regions)
  input = {
    region = each.value
    cidr   = "10.${index(var.regions, each.value)}.0.0/16"
    name   = "${local.name_prefix}-${each.value}"
  }
}

resource "terraform_data" "service" {
  for_each = local.deployments
  input = {
    name     = "${local.name_prefix}-${each.value.name}"
    region   = each.value.region
    image    = each.value.image
    replicas = each.value.replicas
    port     = each.value.port
    cpu      = each.value.cpu
    memory   = each.value.memory
    network  = terraform_data.network[each.value.region].output.name
  }
  triggers_replace = [each.value.image]

  lifecycle {
    precondition {
      condition     = each.value.replicas >= 1
      error_message = "replicas must be at least 1"
    }
  }
}

resource "terraform_data" "dns" {
  for_each = var.services
  input = {
    fqdn    = "${each.key}.${var.environment}.shop.example.com"
    targets = [for region in var.regions : terraform_data.service["${region}/${each.key}"].output.name]
  }
}

check "capacity" {
  assert {
    condition     = local.total_cpu <= 4096
    error_message = "total cpu ${local.total_cpu} exceeds the staging quota of 4096"
  }
}

check "release_candidates" {
  assert {
    condition     = length([for s in var.services : s if can(regex("-rc", s.image))]) == 0
    error_message = "release-candidate images are deployed: ${join(", ", [for s in var.services : s.image if can(regex("-rc", s.image))])}"
  }
}

check "cron_port" {
  assert {
    condition     = alltrue([for s in var.services : s.port > 0])
    error_message = "every service must expose a port"
  }
}

output "deployments" {
  value = { for k, d in terraform_data.service : k => d.output.name }
}

output "capacity" {
  value = {
    cpu    = local.total_cpu
    memory = local.total_memory
  }
}

output "dns" {
  value = { for k, d in terraform_data.dns : k => d.output.fqdn }
}
