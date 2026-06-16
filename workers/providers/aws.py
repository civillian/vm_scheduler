"""
AWS provider — batched per (role_arn, region).

Master AWS credentials are sourced from Vault (KV v2) and used as the
base identity for STS AssumeRole — one assume-role per (role_arn, region)
group, one start/stop call per batch of up to 100 VMs.
Returns per-VM results so the batch collector can track partial failures.
"""

import boto3
import logging
from itertools import islice
from typing import Literal

from workers.vault import get_aws_master_credentials

logger = logging.getLogger(__name__)

# AWS hard limit for instance IDs per StartInstances/StopInstances call
AWS_BATCH_SIZE = 100


def _chunked(iterable, size):
    it = iter(iterable)
    while chunk := list(islice(it, size)):
        yield chunk


def _ec2_client(region: str, role_arn: str | None = None):
    """
    Return an EC2 client. Master credentials are fetched from Vault and
    used to call STS AssumeRole for the target role_arn. If role_arn is
    None, the master credentials are used directly (no assume-role).
    """
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
            DurationSeconds=900,   # 15 min — enough for the batch
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


def aws_batch_action(
    action: Literal["start", "stop"],
    vms: list[dict],   # each: {vm_id, region, role_arn, ...}
) -> dict[str, str]:
    """
    Execute a start or stop action for a list of VMs that share the same
    (role_arn, region). Batches up to 100 IDs per API call.

    Returns a dict of {vm_id: final_state_or_error}.
    """
    if not vms:
        return {}

    # All VMs in this call share the same role/region — take from first entry
    region   = vms[0]["region"]
    role_arn = vms[0].get("role_arn")
    ids      = [vm["vm_id"] for vm in vms]

    logger.info(
        "AWS batch %s: role_arn=%s region=%s count=%d",
        action, role_arn, region, len(ids)
    )

    client = _ec2_client(region, role_arn)
    results: dict[str, str] = {}

    for chunk in _chunked(ids, AWS_BATCH_SIZE):
        try:
            if action == "stop":
                resp = client.stop_instances(InstanceIds=chunk)
                for item in resp["StoppingInstances"]:
                    results[item["InstanceId"]] = item["CurrentState"]["Name"]
            else:
                resp = client.start_instances(InstanceIds=chunk)
                for item in resp["StartingInstances"]:
                    results[item["InstanceId"]] = item["CurrentState"]["Name"]

            logger.info(
                "AWS batch %s chunk complete: role_arn=%s region=%s ids=%s",
                action, role_arn, region, list(results.keys())
            )
        except Exception as exc:
            logger.error(
                "AWS batch %s failed: role_arn=%s region=%s chunk=%s error=%s",
                action, role_arn, region, chunk, exc
            )
            # Mark every VM in the failed chunk so the collector can retry them
            for vm_id in chunk:
                results[vm_id] = f"ERROR: {exc}"

    return results

