variable "name" {
  type = string
}

variable "image" {
  type = string
}

variable "replicas" {
  type = number
}

variable "cpu" {
  type    = number
  default = 256
}

locals {
  container = {
    name  = var.name
    image = var.image
    cpu   = var.cpu
  }
}

output "container_definition" {
  value = jsonencode([local.container])
}
