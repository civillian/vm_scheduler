import os
import redis
from contextlib import contextmanager

LOCK_TTL_SECONDS = 300  # 5 minutes


def _redis_client() -> redis.Redis:
    host     = os.environ.get("REDIS_HOST",     "redis")
    port     = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    db       = int(os.environ.get("REDIS_BROKER_DB", "0"))
    return redis.Redis(host=host, port=port, password=password,
                       db=db, decode_responses=True)


class VmLockError(Exception):
    """Raised when a lock for this VM+action is already held."""
    pass


@contextmanager
def vm_lock(vm_id: str, action: str):
    """
    Acquire a Redis lock for a specific VM and action.
    Raises VmLockError if already locked.
    Uses SET NX EX for atomic acquire-or-fail semantics.
    """
    lock_key = f"vmlock:{vm_id}:{action}"
    client   = _redis_client()
    acquired = client.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        raise VmLockError(f"Lock already held for {vm_id}:{action}")
    try:
        yield
    finally:
        client.delete(lock_key)
