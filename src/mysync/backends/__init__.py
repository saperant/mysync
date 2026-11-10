"""Storage backends."""

from __future__ import annotations

from mysync.backends.base import Backend
from mysync.config import LocationConfig


def open_backend(loc: LocationConfig) -> Backend:
    """Create (but do not connect) the backend for a location."""
    if loc.scheme == "local":
        from mysync.backends.local import LocalBackend
        return LocalBackend(loc)
    if loc.scheme == "sftp":
        from mysync.backends.sftp import SftpBackend
        return SftpBackend(loc)
    if loc.scheme == "ssh":
        from mysync.backends.ssh import SshBackend
        return SshBackend(loc)
    if loc.scheme == "rclone":
        from mysync.backends.rclone import RcloneBackend
        return RcloneBackend(loc)
    raise ValueError(f"unknown location type {loc.scheme!r}")
