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

variable "vm_id" {
  description = "VMware VM instance UUID"
  type        = string
}

variable "vcenter_host" {
  description = "vCenter hostname or IP"
  type        = string
}

variable "power_off_hour" {
  description = "Hour to power off (0-23). Default 0 + power_on_hour default 0 = 24x7 (no scheduling)."
  type    = number
  default = 0
}

variable "power_off_minute" {
  type    = number
  default = 0
}

variable "power_on_hour" {
  description = "Hour to power on (0-23). Default 0 + power_off_hour default 0 = 24x7 (no scheduling)."
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
  description = "Named blackout periods this VM observes. 'weekends' is a built-in period; others are looked up in the central calendar store. Empty list = no blackouts."
  type    = list(string)
  default = ["weekends", "christmas-shutdown", "nat-public-holidays"]
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
    provider         = "vmware"
    vcenter_host     = var.vcenter_host
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
