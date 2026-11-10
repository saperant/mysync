"""The job's ``post_sync_command``: a shell command run after every
synchronization, e.g. to send a notification.  It learns the outcome from
``MYSYNC_*`` environment variables."""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from mysync.config import JobConfig
from mysync.sync import SyncResult

STATUSES = ("ok", "conflicts", "errors", "cancelled", "failed")


def status_of(result: SyncResult | None, conflicts: int) -> str:
    """ok, conflicts (some left unresolved), errors (items failed or changed
    meanwhile, or the state was not saved), cancelled, or failed (the run
    could not start or was aborted)."""
    if result is None:
        return "failed"
    if result.cancelled:
        return "cancelled"
    if result.failed or result.changed or not result.state_saved:
        return "errors"
    return "conflicts" if conflicts else "ok"


def post_sync(job: JobConfig, status: str, result: SyncResult | None = None, conflicts: int = 0,
              log_path: str = "", error: str = "", log: Callable[[str], None] | None = None) -> None:
    """Run the command, if the job has one; problems are only logged."""
    if not job.post_sync_command:
        return
    log = log or (lambda _msg: None)
    if not error and result is not None and result.errors:
        error = result.errors[0]
    env = {
        **os.environ,
        "MYSYNC_JOB": job.name,
        "MYSYNC_STATUS": status,
        "MYSYNC_LEFT": job.left.url,
        "MYSYNC_RIGHT": job.right.url,
        "MYSYNC_DONE": str(result.done if result else 0),
        "MYSYNC_FAILED": str(result.failed if result else 0),
        "MYSYNC_CHANGED": str(result.changed if result else 0),
        "MYSYNC_CONFLICTS": str(conflicts),
        "MYSYNC_BYTES": str(result.bytes if result else 0),
        "MYSYNC_LOG": log_path,
        "MYSYNC_ERROR": error,
    }
    try:
        proc = subprocess.run(["sh", "-c", job.post_sync_command], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, check=False)
    except OSError as e:
        log(f"WARNING post_sync_command could not be started: {e}")
        return
    output = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode:
        log(f"WARNING post_sync_command exited with status {proc.returncode}"
            + (f": {output.splitlines()[-1]}" if output else ""))
    elif output:
        log(f"post_sync_command: {output}")
