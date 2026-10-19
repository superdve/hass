"""Serialize short-lived Tuya TCP connections to each physical device."""

from threading import Lock

_DEVICE_LOCKS: dict[str, Lock] = {}
_REGISTRY_LOCK = Lock()


def device_lock(device_id: str) -> Lock:
    """Return a shared lock for polling and commands running in executors."""
    with _REGISTRY_LOCK:
        return _DEVICE_LOCKS.setdefault(device_id, Lock())
