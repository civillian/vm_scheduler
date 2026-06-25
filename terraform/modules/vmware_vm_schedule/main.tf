terraform {
  required_providers {
    restapi = {
      source  = "mastercard/restapi"
      version = "~> 1.19"
    }
  }
}

variable "scheduler_api_url" {
  type = string
}

variable "power_off_hour" {
  type    = number
  default = 0
}

variable "power_off_minute" {
  type    = number
  default = 0
}

variable "power_on_hour" {
  type    = number
  default = 0
}

variable "power_on_minute" {
  type    = number
  default = 0
}

variable "timezone" {
  type    = string
  default = "Australia/Sydney"
}

variable "blackout_periods" {
  type    = list(string)
  default = ["weekends"]
}

# Resolved by the module — not user-facing
variable "vm_id" {
  description = "VMware VM instance UUID — resolved from state"
  type        = string
}

variable "display_name" {
  description = "Human-readable VM name from the naming service"
  type        = string
}

variable "vcenter_host" {
  description = "vCenter hostname — resolved from state/Vault"
  type        = string
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
    vm_id        = var.vm_id
    display_name = var.display_name
    provider     = "vmware"
    timezone     = var.timezone

    power_off_hour   = var.power_off_hour
    power_off_minute = var.power_off_minute
    power_on_hour    = var.power_on_hour
    power_on_minute  = var.power_on_minute

    blackout_periods = var.blackout_periods

    provider_config = {
      vcenter_host = var.vcenter_host
    }
  })
}

output "vm_id" {
  value = var.vm_id
}
