"""
vault.py — HashiCorp Vault Enterprise credential fetcher.

Supports:
  - KV v2 secrets (AWS master creds, vCenter credentials)
  - Azure secrets engine static roles (client_id / client_secret)
    via raw HTTP since hvac does not yet expose the static-creds endpoint
    for the Azure engine.

Standard Vault env vars (VAULT_ADDR, VAULT_TOKEN, VAULT_NAMESPACE) are
expected to be set by the deployment. hvac picks up VAULT_ADDR and
VAULT_TOKEN automatically; VAULT_NAMESPACE is passed explicitly for
Vault Enterprise.

Per-platform path / role configuration via env vars:

  AWS (KV v2):
    VAULT_AWS_CRED_PATH          e.g. "aws_central_auth/common"
    VAULT_AWS_ACCESS_KEY_FIELD   default "access_key"
    VAULT_AWS_SECRET_KEY_FIELD   default "secret"

  vCenter (KV v2):
    VAULT_VCENTER_CRED_PATH      e.g. "vsphere_auth/common"
    VAULT_VCENTER_USERNAME_FIELD default "username"
    VAULT_VCENTER_PASSWORD_FIELD default "password"

  Azure (static secrets engine):
    VAULT_AZURE_MOUNT            default "azure"
    vault_role and tenant_id come from the VM's provider_config —
    not from env vars, since they differ per subscription/workspace.
"""

from __future__ import annotations

import logging
import os

import hvac

logger = logging.getLogger(__name__)


class VaultConfigError(Exception):
    """Raised when a required Vault env var, secret field, or config is missing."""
    pass


def _client() -> hvac.Client:
    """
    Create an authenticated hvac client from standard Vault env vars.
    Raises VaultConfigError immediately if not authenticated — this is a
    configuration problem that retrying will not fix.
    """
    namespace = os.environ.get("VAULT_NAMESPACE")
    client = hvac.Client(namespace=namespace) if namespace else hvac.Client()

    if not client.is_authenticated():
        raise VaultConfigError(
            "Vault client is not authenticated — check VAULT_ADDR and VAULT_TOKEN"
        )
    return client


def _read_kv2(path: str) -> dict:
    """
    Read a KV v2 secret. Path format: "mount/path/to/secret".
    Splits on the first "/" to determine mount point vs secret path.
    """
    if "/" not in path:
        raise VaultConfigError(
            f"Vault path '{path}' must include a mount point, "
            f"e.g. 'mount/path/to/secret'"
        )
    mount_point, secret_path = path.split("/", 1)

    try:
        response = _client().secrets.kv.v2.read_secret_version(
            mount_point=mount_point,
            path=secret_path,
        )
    except Exception as exc:
        raise VaultConfigError(
            f"Failed to read Vault KV v2 secret at '{path}': {exc}"
        ) from exc

    try:
        return response["data"]["data"]
    except KeyError as exc:
        raise VaultConfigError(
            f"Unexpected Vault response shape for '{path}'"
        ) from exc


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise VaultConfigError(
            f"Required environment variable '{name}' is not set"
        )
    return value


def _field(secret: dict, field_name: str, env_var: str, path: str) -> str:
    if field_name not in secret:
        raise VaultConfigError(
            f"Field '{field_name}' (from {env_var}) not found in "
            f"Vault secret at '{path}'"
        )
    return secret[field_name]


# ---------------------------------------------------------------------------
# AWS — KV v2
# ---------------------------------------------------------------------------

def get_aws_master_credentials() -> tuple[str, str]:
    """
    Fetch master AWS access key / secret key from Vault KV v2.
    Returns (access_key, secret_key).
    """
    path             = _require_env("VAULT_AWS_CRED_PATH")
    access_key_field = os.environ.get("VAULT_AWS_ACCESS_KEY_FIELD", "access_key")
    secret_key_field = os.environ.get("VAULT_AWS_SECRET_KEY_FIELD", "secret")

    secret     = _read_kv2(path)
    access_key = _field(secret, access_key_field, "VAULT_AWS_ACCESS_KEY_FIELD", path)
    secret_key = _field(secret, secret_key_field, "VAULT_AWS_SECRET_KEY_FIELD", path)

    return access_key, secret_key


# ---------------------------------------------------------------------------
# vCenter — KV v2
# ---------------------------------------------------------------------------

def get_vcenter_credentials() -> tuple[str, str]:
    """
    Fetch vCenter username / password from Vault KV v2.
    Returns (username, password).
    """
    path           = _require_env("VAULT_VCENTER_CRED_PATH")
    username_field = os.environ.get("VAULT_VCENTER_USERNAME_FIELD", "username")
    password_field = os.environ.get("VAULT_VCENTER_PASSWORD_FIELD", "password")

    secret   = _read_kv2(path)
    username = _field(secret, username_field, "VAULT_VCENTER_USERNAME_FIELD", path)
    password = _field(secret, password_field, "VAULT_VCENTER_PASSWORD_FIELD", path)

    return username, password


# ---------------------------------------------------------------------------
# Azure — static secrets engine
#
# hvac does not yet expose client.secrets.azure.read_static_credentials(),
# so we call the Vault HTTP API directly via hvac's underlying session.
#
# Vault API endpoint:
#   GET /v1/<mount>/static-creds/<role_name>
#
# Response data fields:
#   client_id, client_secret, expiration, last_vault_rotation,
#   metadata, secret_id
#
# tenant_id is not returned by static-creds — it comes from provider_config
# stored per-VM in vm_schedules, submitted by Terraform at registration time.
# ---------------------------------------------------------------------------

def get_azure_credentials(vault_role: str, tenant_id: str) -> tuple[str, str, str]:
    """
    Fetch Azure client_id and client_secret from the Vault Azure static
    secrets engine for the given role name (typically the Terraform workspace
    name, which maps 1:1 to a subscription).

    Returns (tenant_id, client_id, client_secret).
    tenant_id is passed through from provider_config unchanged.
    """
    if not vault_role:
        raise VaultConfigError(
            "Azure vault_role is not set in provider_config — "
            "ensure the Terraform module submits this at registration time"
        )
    if not tenant_id:
        raise VaultConfigError(
            "Azure tenant_id is not set in provider_config — "
            "ensure the Terraform module submits this at registration time"
        )

    mount = os.environ.get("VAULT_AZURE_MOUNT", "azure")
    client = _client()

    # Raw HTTP call — hvac's underlying session handles auth headers,
    # namespace, and base URL automatically.
    url = f"/v1/{mount}/static-creds/{vault_role}"
    try:
        response = client.auth.client.request(
            "GET",
            url,
            raise_exception_on_none=False,
        )
    except Exception as exc:
        raise VaultConfigError(
            f"Failed to read Azure static-creds for role '{vault_role}': {exc}"
        ) from exc

    if response.status_code != 200:
        raise VaultConfigError(
            f"Vault returned HTTP {response.status_code} for Azure "
            f"static-creds role '{vault_role}': {response.text}"
        )

    try:
        data      = response.json()["data"]
        client_id = data["client_id"]
        client_secret = data["client_secret"]
    except (KeyError, ValueError) as exc:
        raise VaultConfigError(
            f"Unexpected response shape from Azure static-creds "
            f"for role '{vault_role}'"
        ) from exc

    logger.info(
        "Azure credentials fetched from Vault: role=%s client_id=%s",
        vault_role, client_id
    )
    return tenant_id, client_id, client_secret
