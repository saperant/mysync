"""Core data types shared by the backends and the sync engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

FILE = "f"
DIR = "d"
LINK = "l"


@dataclass(slots=True)
class Entry:
    """Attributes of one item as reported by a backend."""

    kind: str  # FILE, DIR or LINK
    size: int = 0
    mtime: float = 0.0
    mode: int | None = None  # permission bits, when the backend knows them
    fid: str | None = None  # stable identity on that storage (inode, cloud file id)
    target: str | None = None  # symbolic links: where the link points

    def to_list(self) -> list:
        return [self.kind, self.size, self.mtime, self.mode, self.fid, self.target]

    @classmethod
    def from_list(cls, data: list) -> "Entry":
        extra = list(data[3:]) + [None] * 3
        return cls(data[0], int(data[1]), float(data[2]), extra[0], extra[1], extra[2])

    def same_as(self, other: "Entry | None") -> bool:
        """Unchanged since it was scanned (used right before modifying it)."""
        if other is None or other.kind != self.kind:
            return False
        if self.kind == FILE:
            return other.size == self.size and abs(other.mtime - self.mtime) < 1e-3
        if self.kind == LINK:
            return other.target == self.target
        return True


class Category(str, Enum):
    """How the two sides relate to each other (what the user sees)."""

    EQUAL = "equal"
    LEFT_ONLY = "left-only"
    RIGHT_ONLY = "right-only"
    LEFT_NEWER = "left-newer"
    RIGHT_NEWER = "right-newer"
    DIFFERENT = "different"
    ATTRIBUTES = "attributes"  # same content, only time/permissions differ
    CONFLICT = "conflict"


class Action(str, Enum):
    """What the engine is going to do with an item."""

    NONE = "none"
    COPY_LR = "copy->"
    COPY_RL = "<-copy"
    MOVE_R = "move-right"  # rename on the right, following a move on the left
    MOVE_L = "move-left"
    ATTR_LR = "attr->"  # content identical: only copy time/permissions
    ATTR_RL = "<-attr"
    DELETE_L = "delete-left"
    DELETE_R = "delete-right"
    # selective sync: remove the local copy, the other side verifiably holds it
    EVICT_L = "free-left"
    EVICT_R = "free-right"
    SKIP = "skip"

    @property
    def is_delete(self) -> bool:
        return self in (Action.DELETE_L, Action.DELETE_R)

    @property
    def is_copy(self) -> bool:
        return self in (Action.COPY_LR, Action.COPY_RL)

    @property
    def is_move(self) -> bool:
        return self in (Action.MOVE_L, Action.MOVE_R)

    @property
    def is_evict(self) -> bool:
        return self in (Action.EVICT_L, Action.EVICT_R)

    @property
    def is_attr(self) -> bool:
        return self in (Action.ATTR_LR, Action.ATTR_RL)

    @property
    def changes_right(self) -> bool:
        return self in (Action.COPY_LR, Action.MOVE_R, Action.ATTR_LR, Action.DELETE_R, Action.EVICT_R)

    @property
    def changes_left(self) -> bool:
        return self in (Action.COPY_RL, Action.MOVE_L, Action.ATTR_RL, Action.DELETE_L, Action.EVICT_L)


class Change(str, Enum):
    """Change of one side relative to the last synchronized state."""

    UNKNOWN = "unknown"  # no previous state available
    UNCHANGED = "unchanged"
    NEW = "new"
    MODIFIED = "modified"
    DELETED = "deleted"
    ABSENT = "absent"  # neither in state nor present now


class SyncError(Exception):
    """An error that aborts a whole comparison or synchronization."""


class ChangedError(OSError):
    """An item changed between comparison and synchronization; it is left alone."""
