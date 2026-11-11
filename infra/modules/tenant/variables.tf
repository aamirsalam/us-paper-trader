variable "name" {
  description = "Tenant name. Every resource name and IAM scope is prefixed with it."
  type        = string
  default     = "us-paper-trader"
}

variable "vpc_cidr" {
  description = "CIDR for this tenant's own VPC. Pick one that doesn't overlap the NSE tenant."
  type        = string
  default     = "10.42.0.0/16"
}

variable "modes" {
  description = "Trading modes; each one is a separate $1,000 Alpaca paper account."
  type        = list(string)
  default     = ["baseline", "conservative", "balanced"]
}

variable "image_tag" {
  description = "ECR tag the scheduled tasks run. CI moves this tag on every deploy."
  type        = string
  default     = "live"
}

variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "schedules_enabled" {
  description = "Keep false until the SSM secrets are filled in and a manual run has passed."
  type        = bool
  default     = false
}

variable "schedules" {
  description = "Phase -> cron in America/New_York. Scheduler handles daylight saving."
  type        = map(string)
  default = {
    premarket = "cron(45 8 ? * MON-FRI *)"
    trade     = "cron(5 10 ? * MON-FRI *)"
    postclose = "cron(30 16 ? * MON-FRI *)"
  }
}

variable "app_env" {
  description = "Extra non-secret environment variables for the container."
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "create_budget" {
  description = "Monthly budget alarm for this tenant. AWS gives two budgets per account free; extra ones cost ~$0.60/month."
  type        = bool
  default     = true
}

variable "budget_usd" {
  type    = number
  default = 3
}

variable "alert_email" {
  description = "Where budget alerts go."
  type        = string
  default     = ""
}

variable "activate_cost_tag" {
  description = "Activate the `project` cost-allocation tag so the budget can filter on it. Turn on once the tag has shown up in billing (about a day after first deploy)."
  type        = bool
  default     = false
}

variable "github_repo" {
  description = "owner/repo allowed to push images via OIDC. Empty disables the deploy role."
  type        = string
  default     = "aamirsalam/us-paper-trader"
}

variable "github_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN in the account (one per account; the NSE stack may already have it). Leave empty and set create_github_oidc_provider = true to create one."
  type        = string
  default     = ""
}

variable "create_github_oidc_provider" {
  type    = bool
  default = false
}
