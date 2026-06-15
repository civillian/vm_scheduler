import redis
from contextlib import contextmanager

redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)

LOCK_TTL_SECONDS = 300  # 5 minutes — long enough for any VM op to complete


class VmLockError(Exception):
    """Raised when a lock for this VM+action is already held."""
    pass


@contextmanager
def vm_lock(vm_id: str, action: str):
    """
    Acquire a Redis lock for a specific VM and action.
    Raises VmLockError if already locked (i.e. a prior task is still running).
    Uses SET NX EX for atomic acquire-or-fail semantics.
    """
    lock_key = f"vmlock:{vm_id}:{action}"
    acquired = redis_client.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        raise VmLockError(f"Lock already held for {vm_id}:{action}")
    try:
        yield
    finally:
        redis_client.delete(lock_key)
