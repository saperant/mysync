"""Loading and validating job configuration files.

Every ``*.toml`` file in ``~/.config/mysync/conf.d/`` describes one sync job.
An optional ``~/.config/mysync/mysync.toml`` provides defaults: its top level
keys apply to every job, and its ``[location]`` table to every location.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from mysync import paths

MODES = ("two-way", "mirror", "update")
COMPARE_METHODS = ("time-size", "size", "content")
CONFLICT_POLICIES = ("ask", "skip", "newer", "left", "right")
DELETION_POLICIES = ("trash", "delete")
SYMLINK_POLICIES = ("skip", "follow", "links")

DEFAULT_STATE_IN_LOCATION = ".mysync"
DEFAULT_TRASH_DIR = ".mysync/trash"

_JOB_KEYS = {
    "name", "description", "mode", "compare", "time_tolerance", "conflict", "deletion",
    "symlinks", "max_delete_percent", "trash_keep_days", "include", "exclude",
    "detect_moves", "verify_before_copy", "sync_permissions", "transfers",
    "delta_transfer", "delta_min_size", "selective", "left", "right",
    "case_sensitive", "unicode_normalization", "ignore_time_shift", "retries", "retry_delay",
    "post_sync_command",
}
_LOCATION_KEYS = {
    "url", "state_dir", "trash_dir", "identity_file", "known_hosts", "host_key_policy",
    "ssh_command", "ssh_options", "rclone", "rclone_flags", "connect_timeout", "remote_hash",
}


class ConfigError(Exception):
    pass


@dataclass
class LocationConfig:
    url: str
    scheme: str  # "local", "sftp", "ssh" or "rclone"
    path: str
    host: str = ""
    user: str | None = None
    port: int | None = None
    state_dir: str = ""
    trash_dir: str = DEFAULT_TRASH_DIR
    options: dict = field(default_factory=dict)

    @property
    def state_in_location(self) -> bool:
        """State lives inside the location when ``state_dir`` is a relative path."""
        return not (self.state_dir.startswith("~") or os.path.isabs(self.state_dir))

    @property
    def local_state_dir(self) -> Path:
        return Path(self.state_dir).expanduser()

    @property
    def key(self) -> str:
        """Normalized identity of the location (independent of URL spelling)."""
        return f"{self.scheme}://{self.user or ''}@{self.host}:{self.port or ''}/{self.path}"


@dataclass
class JobConfig:
    name: str
    left: LocationConfig
    right: LocationConfig
    file: Path | None = None
    description: str = ""
    mode: str = "two-way"
    compare: str = "time-size"
    time_tolerance: float = 2.0
    conflict: str = "ask"
    deletion: str = "trash"
    symlinks: str = "skip"
    max_delete_percent: float = 50.0
    trash_keep_days: int = 30
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    detect_moves: bool = True
    verify_before_copy: bool = True
    sync_permissions: bool = False
    transfers: int = 4
    delta_transfer: bool = True
    delta_min_size: int = 4 * 1024 * 1024
    selective: str = ""  # "left"/"right": that side keeps only the selected parts
    case_sensitive: bool | None = None  # None: detect (insensitive if either side is)
    unicode_normalization: bool = True  # "é" composed or decomposed is the same name
    ignore_time_shift: list[float] = field(default_factory=list)  # whole shifts in hours
    retries: int = 2  # rounds in which failed items are tried again
    retry_delay: float = 5.0  # seconds before each round
    post_sync_command: str = ""  # shell command run after every synchronization

    @property
    def pair_id(self) -> str:
        """Stable identifier of the folder pair, used to name state files."""
        key = f"{self.left.key}\n{self.right.key}".encode()
        return hashlib.sha256(key).hexdigest()[:16]


def parse_location_url(url: str) -> tuple[str, str, str, str | None, int | None]:
    """Return ``(scheme, path, host, user, port)`` for a location URL.

    Accepted forms::

        /abs/path  ~/path  file:///abs/path
        sftp://[user@]host[:port]/abs/path     (``/~/dir`` is relative to home)
        ssh://[user@]host[:port]/abs/path
        rclone:remote:path                      (any rclone path)
    """
    if url.startswith("rclone:"):
        rest = url[len("rclone:"):]
        if not rest:
            raise ConfigError(f"empty rclone path in {url!r}")
        return "rclone", rest, "", None, None
    if url.startswith("file://"):
        return "local", os.path.expanduser(unquote(url[len("file://"):])), "", None, None
    if url.startswith(("sftp://", "ssh://")):
        parts = urlsplit(url)
        if not parts.hostname:
            raise ConfigError(f"missing host in {url!r}")
        path = unquote(parts.path) or "/"
        if path.startswith("/~"):
            path = path[1:]
        try:
            port = parts.port
        except ValueError as e:
            raise ConfigError(f"bad port in {url!r}") from e
        user = unquote(parts.username) if parts.username else None
        return parts.scheme, path, parts.hostname, user, port
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        raise ConfigError(f"unsupported URL scheme in {url!r} (use a path, sftp://, ssh:// or rclone:)")
    return "local", os.path.abspath(os.path.expanduser(url)), "", None, None


def _location(raw, side: str, defaults: dict) -> LocationConfig:
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict) or "url" not in raw:
        raise ConfigError(f"[{side}] needs a 'url'")
    data = {**defaults, **raw}
    unknown = set(data) - _LOCATION_KEYS
    if unknown:
        raise ConfigError(f"[{side}] unknown key(s): {', '.join(sorted(unknown))}")
    scheme, path, host, user, port = parse_location_url(str(data["url"]))
    loc = LocationConfig(
        url=str(data["url"]),
        scheme=scheme,
        path=path,
        host=host,
        user=user,
        port=port,
        state_dir=str(data.get("state_dir") or paths.default_state_dir()),
        trash_dir=str(data.get("trash_dir") or DEFAULT_TRASH_DIR).strip("/"),
        options={k: v for k, v in data.items() if k not in ("url", "state_dir", "trash_dir")},
    )
    if loc.state_in_location:
        loc.state_dir = loc.state_dir.strip("/") or DEFAULT_STATE_IN_LOCATION
    return loc


def _choice(data: dict, key: str, choices: tuple[str, ...], default: str) -> str:
    value = data.get(key, default)
    if value not in choices:
        raise ConfigError(f"'{key}' must be one of {', '.join(choices)} (got {value!r})")
    return value


def _bool(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be true or false")
    return value


def _size(data: dict, key: str, default: int) -> int:
    """A byte count: an integer or a string like "512K", "4M", "1G"."""
    value = data.get(key, default)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    m = re.fullmatch(r"\s*(\d+)\s*([KMG]?)i?B?\s*", str(value), re.IGNORECASE)
    if not m:
        raise ConfigError(f"'{key}' must be a size such as 4M (got {value!r})")
    return int(m.group(1)) * 1024 ** " KMG".index(m.group(2).upper() or " ")


def _case(data: dict) -> bool | None:
    value = data.get("case_sensitive", "auto")
    if value == "auto":
        return None
    if not isinstance(value, bool):
        raise ConfigError("'case_sensitive' must be true, false or \"auto\"")
    return value


def _hours(data: dict, key: str) -> list[float]:
    value = data.get(key, [])
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value]
    def hours(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 24
    if not isinstance(value, list) or not all(hours(v) for v in value):
        raise ConfigError(f"'{key}' must be a list of hours between 0 and 24, e.g. [1]")
    return [float(v) for v in value]


def _str_list(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{key}' must be a list of strings")
    return list(value)


def job_from_dict(data: dict, name: str, file: Path | None = None, defaults: dict | None = None) -> JobConfig:
    defaults = dict(defaults or {})
    loc_defaults = defaults.pop("location", {}) or {}
    merged = {**defaults, **data}
    unknown = set(merged) - _JOB_KEYS
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(sorted(unknown))}")
    for side in ("left", "right"):
        if side not in merged:
            raise ConfigError(f"missing [{side}] location")
    try:
        job = JobConfig(
            name=str(merged.get("name") or name),
            file=file,
            description=str(merged.get("description", "")),
            left=_location(merged["left"], "left", loc_defaults),
            right=_location(merged["right"], "right", loc_defaults),
            mode=_choice(merged, "mode", MODES, "two-way"),
            compare=_choice(merged, "compare", COMPARE_METHODS, "time-size"),
            time_tolerance=float(merged.get("time_tolerance", 2.0)),
            conflict=_choice(merged, "conflict", CONFLICT_POLICIES, "ask"),
            deletion=_choice(merged, "deletion", DELETION_POLICIES, "trash"),
            symlinks=_choice(merged, "symlinks", SYMLINK_POLICIES, "skip"),
            max_delete_percent=float(merged.get("max_delete_percent", 50.0)),
            trash_keep_days=int(merged.get("trash_keep_days", 30)),
            include=_str_list(merged, "include"),
            exclude=_str_list(merged, "exclude"),
            detect_moves=_bool(merged, "detect_moves", True),
            verify_before_copy=_bool(merged, "verify_before_copy", True),
            sync_permissions=_bool(merged, "sync_permissions", False),
            transfers=int(merged.get("transfers", 4)),
            delta_transfer=_bool(merged, "delta_transfer", True),
            delta_min_size=_size(merged, "delta_min_size", 4 * 1024 * 1024),
            selective=_choice(merged, "selective", ("", "left", "right"), ""),
            case_sensitive=_case(merged),
            unicode_normalization=_bool(merged, "unicode_normalization", True),
            ignore_time_shift=_hours(merged, "ignore_time_shift"),
            retries=int(merged.get("retries", 2)),
            retry_delay=float(merged.get("retry_delay", 5.0)),
            post_sync_command=str(merged.get("post_sync_command", "")),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(str(e)) from e
    for loc in (job.left, job.right):
        loc.options["_symlinks"] = job.symlinks  # backends started as processes need it up front
    if not 1 <= job.transfers <= 64:
        raise ConfigError("'transfers' must be between 1 and 64")
    if not 0 <= job.retries <= 100 or job.retry_delay < 0:
        raise ConfigError("'retries' must be between 0 and 100 and 'retry_delay' must not be negative")
    if job.left.key == job.right.key:
        raise ConfigError("left and right are the same location")
    if job.left.scheme == job.right.scheme == "local":
        a, b = Path(job.left.path), Path(job.right.path)
        if a in b.parents or b in a.parents:
            raise ConfigError("left and right must not be nested inside each other")
    return job


def load_global_defaults() -> dict:
    path = paths.global_config_file()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_job_file(path: Path, defaults: dict | None = None) -> JobConfig:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from e
    try:
        return job_from_dict(data, path.stem, path, defaults)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


@dataclass
class LoadResult:
    jobs: list[JobConfig]
    errors: list[str]


def load_all(directory: Path | None = None) -> LoadResult:
    directory = directory or paths.conf_dir()
    jobs: list[JobConfig] = []
    errors: list[str] = []
    try:
        defaults = load_global_defaults()
    except ConfigError as e:
        errors.append(str(e))
        defaults = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.toml")):
            try:
                jobs.append(load_job_file(path, defaults))
            except ConfigError as e:
                errors.append(str(e))
    names: dict[str, Path | None] = {}
    for job in jobs:
        if job.name in names:
            errors.append(f"duplicate job name {job.name!r} in {job.file} and {names[job.name]}")
        names[job.name] = job.file
    return LoadResult(jobs, errors)


def find_job(name: str, directory: Path | None = None) -> JobConfig:
    """Find a job by name, or load it directly when ``name`` is a TOML file path."""
    candidate = Path(name).expanduser()
    if name.endswith(".toml") and candidate.is_file():
        return load_job_file(candidate, load_global_defaults())
    result = load_all(directory)
    for job in result.jobs:
        if job.name == name:
            return job
    detail = f" ({'; '.join(result.errors)})" if result.errors else ""
    raise ConfigError(f"no job named {name!r}{detail}")


TEMPLATE = '''\
# mysync job "{name}"
# Locations: a local path, sftp://[user@]host[:port]/path, ssh://[user@]host/path
# or rclone:remote:path (requires the rclone binary; used only as transport).

mode = "two-way"          # two-way | mirror (left -> right) | update (left -> right, no deletions)
compare = "time-size"     # time-size | size | content
time_tolerance = 2        # seconds of modification time difference treated as equal
conflict = "ask"          # ask | skip | newer | left | right
deletion = "trash"        # trash (move into <location>/.mysync/trash/<time>/) | delete
trash_keep_days = 30      # purge trash sessions older than this (0 = keep forever)
symlinks = "skip"         # skip | follow | links (relative links pointing inside the tree are
                          # recreated as links; other links are skipped and reported)
transfers = 4             # files copied in parallel
delta_transfer = true     # for changed files on ssh/sftp/local send only the changed blocks
delta_min_size = "4M"     # ... when the file is at least this big
max_delete_percent = 50   # refuse to sync if more of a side would be deleted (0 = no limit)
detect_moves = true       # replay renames/moves instead of deleting and transferring again
verify_before_copy = true # checksum same-size files where the data lives; identical ones only get
                          # their time/permissions updated (used when both sides can hash remotely)
sync_permissions = false  # also compare and copy permission bits (local, sftp, ssh)
case_sensitive = "auto"   # auto | true | false: whether "Foo.txt" and "foo.txt" are different files;
                          # auto: not when either side is case-insensitive (OneDrive, exFAT, macOS, ...)
unicode_normalization = true  # "é" composed or decomposed (macOS) is the same name
ignore_time_shift = []    # e.g. [1]: times differing by exactly 1 hour (FAT disks and DST) are equal
retries = 2               # failed items are tried again this many times ...
retry_delay = 5           # ... after waiting this many seconds
# post_sync_command = 'notify-send "mysync $MYSYNC_JOB" "$MYSYNC_STATUS"'   # run after every sync
# selective = "left"      # selective sync: the left side keeps only what `mysync select` chose,
                          # the right side holds everything

exclude = [".git/", "*.tmp", "~$*", ".DS_Store", "Thumbs.db"]
include = []              # when non-empty only matching files are synchronized

[left]
url = "{left}"
# Where the synchronization state of this side is kept.  An absolute or ~ path
# is a directory on this machine; a relative path is inside the location itself.
# state_dir = "~/.config/mysync/db"

[right]
url = "{right}"
# state_dir = ".mysync"
# identity_file = "~/.ssh/id_ed25519"   # sftp: key to use (agent and ~/.ssh/config are honoured)
# host_key_policy = "strict"            # sftp/ssh: strict | accept-new
'''


def write_template(name: str, left: str = "~/Documents", right: str = "sftp://user@host/home/user/Documents") -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError("job names may contain only letters, digits, '.', '_' and '-'")
    directory = paths.conf_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    if path.exists():
        raise ConfigError(f"{path} already exists")
    path.write_text(TEMPLATE.format(name=name, left=left, right=right))
    return path
