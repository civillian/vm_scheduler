terraform {
  required_providers {
    restapi = {
      source  = "mastercard/restapi"
      version = "~> 1.19"
    }
  }
}

variable "scheduler_api_url" {
  description = "Base URL of the vm-scheduler API (internal cluster DNS)"
  type        = string
}

variable "vm_id" {
  description = "AWS EC2 instance ID"
  type        = string
}

variable "region" {
  description = "AWS region the instance lives in (retained for future multi-region support)"
  type        = string
}

variable "role_arn" {
  description = "IAM role ARN the scheduler will assume to operate on this instance. Encodes the account implicitly and scopes batch grouping — each role should be limited to the instances of its own workload/workspace."
  type        = string
  # e.g. "arn:aws:iam::123456789012:role/vm-scheduler-role"
}

variable "power_off_hour" {
  description = "Hour to power off (0-23, in the specified timezone). Default 0 + power_on_hour default 0 = 24x7 (no scheduling)."
  type        = number
  default     = 0
}

variable "power_off_minute" {
  description = "Minute to power off (0-59)"
  type        = number
  default     = 0
}

variable "power_on_hour" {
  description = "Hour to power on (0-23, in the specified timezone). Default 0 + power_off_hour default 0 = 24x7 (no scheduling)."
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
  description = "Named blackout periods this VM observes. 'weekends' is a built-in period; others are looked up in the central calendar store. Empty list = no blackouts."
  type        = list(string)
  default     = ["weekends", "christmas-shutdown", "nat-public-holidays"]
}

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
    vm_id            = var.vm_id
    provider         = "aws"
    region           = var.region
    role_arn         = var.role_arn
    power_off_hour   = var.power_off_hour
    power_off_minute = var.power_off_minute
    power_on_hour    = var.power_on_hour
    power_on_minute  = var.power_on_minute
    timezone         = var.timezone
    blackout_periods = var.blackout_periods
  })
}

output "vm_id" {
  value = var.vm_id
}

output "schedule_summary" {
  value = var.power_on_hour == 0 && var.power_off_hour == 0 ? "24x7 (no scheduling)" : "off=${var.power_off_hour}:${format("%02d", var.power_off_minute)} on=${var.power_on_hour}:${format("%02d", var.power_on_minute)} tz=${var.timezone}"
}
