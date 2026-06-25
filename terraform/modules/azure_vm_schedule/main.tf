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
  type = string
}

variable "display_name" {
  type = string
}

variable "tenant_id" {
  description = "Azure tenant ID — org-wide constant, resolved from Vault/state"
  type        = string
}

variable "subscription_id" {
  description = "Azure subscription ID — resolved from state"
  type        = string
}

variable "resource_group" {
  description = "Azure resource group — resolved from state"
  type        = string
}

# vault_role maps to terraform.workspace — each workspace corresponds
# to one subscription with its own Vault Azure static role.
variable "vault_role" {
  description = "Vault Azure static role name — use terraform.workspace"
  type        = string
  default     = terraform.workspace
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
    provider     = "azure"
    timezone     = var.timezone

    power_off_hour   = var.power_off_hour
    power_off_minute = var.power_off_minute
    power_on_hour    = var.power_on_hour
    power_on_minute  = var.power_on_minute

    blackout_periods = var.blackout_periods

    provider_config = {
      tenant_id       = var.tenant_id
      subscription_id = var.subscription_id
      resource_group  = var.resource_group
      vault_role      = var.vault_role
    }
  })
}

output "vm_id" {
  value = var.vm_id
}
