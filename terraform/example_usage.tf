# ---------------------------------------------------------------------------
# 24x7 — prod server, omit all scheduling vars entirely.
# Both hours default to 0 which is the 24x7 sentinel.
# ---------------------------------------------------------------------------

module "prod_server_aws" {
  source = "git::https://your-tfe-instance/modules/aws_vm_schedule"
  # No schedule vars — this VM runs 24x7
}


# ---------------------------------------------------------------------------
# Business hours — most common case for non-prod.
# Default blackout_periods covers weekends, Christmas shutdown, and national public holidays.
# ---------------------------------------------------------------------------

module "dev_server_aws" {
  source = "git::https://your-tfe-instance/modules/aws_vm_schedule"

  power_on_hour  = 7
  power_off_hour = 19
}


# ---------------------------------------------------------------------------
# Custom hours with explicit blackout periods.
# ---------------------------------------------------------------------------

module "batch_server_aws" {
  source = "git::https://your-tfe-instance/modules/aws_vm_schedule"

  power_on_hour    = 6
  power_off_hour   = 22
  blackout_periods = ["weekends", "christmas-shutdown", "nat-public-holidays"]
}


# ---------------------------------------------------------------------------
# A VM that runs 7 days a week but still observes Christmas shutdown.
# Omit "weekends" from the list.
# ---------------------------------------------------------------------------

module "weekend_batch_server" {
  source = "git::https://your-tfe-instance/modules/aws_vm_schedule"

  power_on_hour    = 6
  power_off_hour   = 20
  blackout_periods = ["christmas-shutdown", "nat-public-holidays"]
}


# ---------------------------------------------------------------------------
# A VM with no blackouts at all — runs on schedule every day of the year.
# ---------------------------------------------------------------------------

module "always_scheduled_server" {
  source = "git::https://your-tfe-instance/modules/aws_vm_schedule"

  power_on_hour    = 8
  power_off_hour   = 18
  blackout_periods = []
}


# ---------------------------------------------------------------------------
# Azure and VMware follow the same interface.
# ---------------------------------------------------------------------------

module "dev_server_azure" {
  source = "git::https://your-tfe-instance/modules/azure_vm_schedule"

  power_on_hour  = 7
  power_off_hour = 19
  timezone       = "Australia/Perth"
}

module "dev_server_vmware" {
  source = "git::https://your-tfe-instance/modules/vmware_vm_schedule"

  power_on_hour  = 7
  power_off_hour = 19
}
