terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial config: bucket comes from backend.hcl so this tenant's state lives under its own key,
  # separate from the NSE stack's state. `terraform init -backend-config=backend.hcl`
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "us-paper-trader"
      tenant     = "us"
      managed_by = "terraform"
      repo       = "aamirsalam/us-paper-trader"
    }
  }
}

module "tenant" {
  source = "../../modules/tenant"

  name                        = "us-paper-trader"
  vpc_cidr                    = var.vpc_cidr
  modes                       = var.modes
  schedules_enabled           = var.schedules_enabled
  alert_email                 = var.alert_email
  budget_usd                  = var.budget_usd
  create_budget               = var.create_budget
  activate_cost_tag           = var.activate_cost_tag
  github_oidc_provider_arn    = var.github_oidc_provider_arn
  create_github_oidc_provider = var.create_github_oidc_provider
  app_env                     = var.app_env
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "modes" {
  type    = list(string)
  default = ["baseline", "conservative", "balanced", "aggressive"]
}

variable "schedules_enabled" {
  type    = bool
  default = false
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "budget_usd" {
  type    = number
  default = 3
}

variable "create_budget" {
  type    = bool
  default = true
}

variable "activate_cost_tag" {
  type    = bool
  default = false
}

variable "github_oidc_provider_arn" {
  type    = string
  default = ""
}

variable "create_github_oidc_provider" {
  type    = bool
  default = false
}

variable "app_env" {
  type    = map(string)
  default = {}
}

output "tenant" {
  value = module.tenant
}
