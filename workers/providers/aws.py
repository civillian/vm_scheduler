"""
AWS provider — batched per (role_arn, region).

Credentials flow:
  1. Fetch master AWS access/secret key from Vault KV v2
  2. Use those to call STS AssumeRole for the per-workload role_arn
  3. Use assumed credentials for EC2 start/stop batch call

All provider-specific fields come from vm.provider_config:
  {"role_arn": "arn:aws:iam::112233445566:role/vm-scheduler",
   "region":   "ap-southeast-2"}
"""

import boto3
import logging
from itertools import islice

from workers.vault import get_aws_master_credentials

logger = logging.getLogger(__name__)

AWS_BATCH_SIZE = 100


def _chunked(iterable, size):
    it = iter(iterable)
    while chunk := list(islice(it, size)):
        yield chunk


def _ec2_client(region: str, role_arn: str | None = None):
    access_key, secret_key = get_aws_master_credentials()

    if role_arn:
        sts = boto3.client(
            "sts",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="vm-scheduler",
            DurationSeconds=900,
        )
        creds = assumed["Credentials"]
        return boto3.client(
            "ec2",
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    return boto3.client(
        "ec2",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def aws_batch_action(action: str, vms: list[dict]) -> dict[str, str]:
    """
    Execute power_on or power_off for a list of VMs sharing the same
    (role_arn, region). Batches up to 100 instance IDs per API call.
    Returns {vm_id: detail_or_error}.
    """
    if not vms:
        return {}

    config   = vms[0].get("provider_config") or {}
    region   = config.get("region", "")
    role_arn = config.get("role_arn")
    ids      = [vm["vm_id"] for vm in vms]

    logger.info(
        "AWS batch power_%s: role_arn=%s region=%s count=%d",
        action, role_arn, region, len(ids)
    )

    client  = _ec2_client(region, role_arn)
    results: dict[str, str] = {}

    for chunk in _chunked(ids, AWS_BATCH_SIZE):
        try:
            if action == "off":
                resp = client.stop_instances(InstanceIds=chunk)
                for item in resp["StoppingInstances"]:
                    results[item["InstanceId"]] = (
                        f"stop accepted — transitioning to "
                        f"{item['CurrentState']['Name']}"
                    )
            else:
                resp = client.start_instances(InstanceIds=chunk)
                for item in resp["StartingInstances"]:
                    results[item["InstanceId"]] = (
                        f"start accepted — transitioning to "
                        f"{item['CurrentState']['Name']}"
                    )
            logger.info(
                "AWS batch power_%s chunk complete: role_arn=%s region=%s ids=%s",
                action, role_arn, region, list(results.keys())
            )
        except Exception as exc:
            logger.error(
                "AWS batch power_%s failed: role_arn=%s region=%s error=%s",
                action, role_arn, region, exc
            )
            for vm_id in chunk:
                results[vm_id] = f"ERROR: {exc}"

    return results
