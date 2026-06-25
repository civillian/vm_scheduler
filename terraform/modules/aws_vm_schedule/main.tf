terraform {
  required_providers {
    restapi = {
      source  = "mastercard/restapi"
      version = "~> 1.19"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables — user-facing (intent only)
# ---------------------------------------------------------------------------

variable "scheduler_api_url" {
  description = "Base URL of the vm-scheduler API (internal cluster DNS)"
  type        = string
}

variable "power_off_hour" {
  description = "Hour to power off (0-23). Default 0 + power_on_hour 0 = 24x7."
  type        = number
  default     = 0
}

variable "power_off_minute" {
  description = "Minute to power off (0-59)"
  type        = number
  default     = 0
}

variable "power_on_hour" {
  description = "Hour to power on (0-23). Default 0 + power_off_hour 0 = 24x7."
  type        = number
  default     = 0
}

variable "power_on_minute" {
  description = "Minute to power on (0-59)"
  type        = number
  default     = 0
}

variable "timezone" {
  description = "IANA timezone (e.g. Australia/Sydney)"
  type        = string
  default     = "Australia/Sydney"
}

variable "blackout_periods" {
  description = "Named blackout periods. 'weekends' is built-in; others are calendar names."
  type        = list(string)
  default     = ["weekends"]
}

# ---------------------------------------------------------------------------
# Resolved by the module from Terraform state / Vault — not user-facing
# ---------------------------------------------------------------------------

variable "vm_id" {
  description = "EC2 instance ID — resolved from terraform state"
  type        = string
}

variable "display_name" {
  description = "Human-readable VM name from the naming service"
  type        = string
  # source: module.naming-service.generated_vm_name
}

variable "role_arn" {
  description = "IAM role ARN — resolved from Vault by the module"
  type        = string
}

variable "region" {
  description = "AWS region — resolved from provider config"
  type        = string
}

# ---------------------------------------------------------------------------

provider "restapi" {
  uri                  = var.scheduler_api_url
  write_returns_object = true
}

resource "restapi_object" "vm_schedule" {
  path         = "/schedule"
  update_path  = "/schedule/${var.vm_id}"
  destroy_path = "/schedule/${var.vm_id}"
  id_attribute = "vm_id"

  data = jsonencode({
    vm_id        = var.vm_id
    display_name = var.display_name
    provider     = "aws"
    timezone     = var.timezone

    power_off_hour   = var.power_off_hour
    power_off_minute = var.power_off_minute
    power_on_hour    = var.power_on_hour
    power_on_minute  = var.power_on_minute

    blackout_periods = var.blackout_periods

    provider_config = {
      role_arn = var.role_arn
      region   = var.region
    }
  })
}

output "vm_id" {
  value = var.vm_id
}

output "schedule_summary" {
  value = var.power_on_hour == 0 && var.power_off_hour == 0 ? "24x7" : "off=${var.power_off_hour}:${format("%02d", var.power_off_minute)} on=${var.power_on_hour}:${format("%02d", var.power_on_minute)} tz=${var.timezone}"
}
