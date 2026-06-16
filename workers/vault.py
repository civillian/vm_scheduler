"""
vault.py — shared HashiCorp Vault Enterprise (KV v2) credential fetcher.

Standard Vault env vars are expected to be set by the deployment
(VAULT_ADDR, VAULT_TOKEN, VAULT_NAMESPACE) — hvac.Client() picks up
VAULT_ADDR and VAULT_TOKEN automatically; VAULT_NAMESPACE is passed
explicitly for Enterprise.

Per-platform path and field names are configured via env vars so the
KV layout can change without code changes:

  AWS:
    VAULT_AWS_CRED_PATH        e.g. "aws_central_auth/common"
    VAULT_AWS_ACCESS_KEY_FIELD default "access_key"
    VAULT_AWS_SECRET_KEY_FIELD default "secret"

  vCenter:
    VAULT_VCENTER_CRED_PATH        e.g. "vsphere_auth/common"
    VAULT_VCENTER_USERNAME_FIELD   default "username"
    VAULT_VCENTER_PASSWORD_FIELD   default "password"

A secret is fetched fresh on every call — no caching. Given batching
means at most one fetch per (role_arn, region) group per minute for
AWS, and one fetch per vCenter loop for VMware, the added Vault round
trip is negligible relative to the AWS/vCenter API calls themselves.
"""

from __future__ import annotations

import logging
import os

import hvac

logger = logging.getLogger(__name__)


class VaultConfigError(Exception):
    """Raised when a required Vault env var or secret field is missing."""
    pass


def _client() -> hvac.Client:
    """
    Create an hvac client from standard Vault env vars.
    VAULT_ADDR and VAULT_TOKEN are picked up automatically by hvac;
    VAULT_NAMESPACE (Enterprise) is passed explicitly if set.
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
    Read a KV v2 secret at the given path (mount-relative, e.g.
    "aws_central_auth/common"). Splits on the first "/" to determine
    the mount point vs the secret path within it.
    """
    client = _client()

    if "/" not in path:
        raise VaultConfigError(
            f"Vault path '{path}' must include a mount point, e.g. 'mount/path/to/secret'"
        )
    mount_point, secret_path = path.split("/", 1)

    try:
        response = client.secrets.kv.v2.read_secret_version(
            mount_point=mount_point,
            path=secret_path,
        )
    except Exception as exc:
        raise VaultConfigError(f"Failed to read Vault secret at '{path}': {exc}") from exc

    try:
        return response["data"]["data"]
    except KeyError as exc:
        raise VaultConfigError(f"Unexpected Vault response shape for '{path}'") from exc


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise VaultConfigError(f"Required environment variable '{name}' is not set")
    return value


def _field(secret: dict, field_name: str, env_var: str, path: str) -> str:
    if field_name not in secret:
        raise VaultConfigError(
            f"Field '{field_name}' (from {env_var}) not found in Vault secret at '{path}'"
        )
    return secret[field_name]


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------

def get_aws_master_credentials() -> tuple[str, str]:
    """
    Fetch the master AWS access key / secret key from Vault.
    Returns (access_key, secret_key).
    """
    path = _require_env("VAULT_AWS_CRED_PATH")
    access_key_field = os.environ.get("VAULT_AWS_ACCESS_KEY_FIELD", "access_key")
    secret_key_field = os.environ.get("VAULT_AWS_SECRET_KEY_FIELD", "secret")

    secret = _read_kv2(path)

    access_key = _field(secret, access_key_field, "VAULT_AWS_ACCESS_KEY_FIELD", path)
    secret_key = _field(secret, secret_key_field, "VAULT_AWS_SECRET_KEY_FIELD", path)

    return access_key, secret_key


# ---------------------------------------------------------------------------
# vCenter
# ---------------------------------------------------------------------------

def get_vcenter_credentials() -> tuple[str, str]:
    """
    Fetch vCenter username / password from Vault.
    Returns (username, password).
    """
    path = _require_env("VAULT_VCENTER_CRED_PATH")
    username_field = os.environ.get("VAULT_VCENTER_USERNAME_FIELD", "username")
    password_field = os.environ.get("VAULT_VCENTER_PASSWORD_FIELD", "password")

    secret = _read_kv2(path)

    username = _field(secret, username_field, "VAULT_VCENTER_USERNAME_FIELD", path)
    password = _field(secret, password_field, "VAULT_VCENTER_PASSWORD_FIELD", path)

    return username, password
