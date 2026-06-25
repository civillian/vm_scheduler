# ---------------------------------------------------------------------------
# Example usage — what a consumer's Terraform workspace looks like.
# All infrastructure context is resolved by the module internally.
# The user only expresses scheduling intent.
# ---------------------------------------------------------------------------

# 24x7 server — omit all schedule vars entirely
module "always_on_server" {
  source            = "git::https://your-tfe-instance/modules/aws_vm_schedule"
  # vm_id, display_name, role_arn, region resolved internally by the module
}

# Business hours dev server — default blackout_periods apply
module "dev_server" {
  source         = "git::https://your-tfe-instance/modules/aws_vm_schedule"
  power_on_hour  = 7
  power_off_hour = 19
}

# Custom hours, extended blackout list
module "batch_server" {
  source           = "git::https://your-tfe-instance/modules/aws_vm_schedule"
  power_on_hour    = 6
  power_off_hour   = 22
  blackout_periods = ["weekends", "christmas-shutdown", "nat-public-holidays"]
}

# 7-day schedule, Christmas only
module "weekend_batch_server" {
  source           = "git::https://your-tfe-instance/modules/aws_vm_schedule"
  power_on_hour    = 6
  power_off_hour   = 20
  blackout_periods = ["christmas-shutdown"]
}

# Azure — vault_role defaults to terraform.workspace automatically
module "azure_dev_server" {
  source         = "git::https://your-tfe-instance/modules/azure_vm_schedule"
  power_on_hour  = 7
  power_off_hour = 19
  timezone       = "Australia/Perth"
}

# VMware
module "vmware_dev_server" {
  source         = "git::https://your-tfe-instance/modules/vmware_vm_schedule"
  power_on_hour  = 7
  power_off_hour = 19
}
