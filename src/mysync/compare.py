"""Comparison: scan both sides, relate them to the previous state and decide
what to do with every item.

Two-way decisions are a three-way merge between the left side, the right side
and the state recorded after the last sync.  Without that state a deletion
looks exactly like a new file on the other side; with it, "deleted on the
left, unchanged on the right" can be propagated as a deletion.

The plan is then optimized so that nothing is transferred needlessly:

* "delete X on the right" + "copy Y to the right" where the right's X has the
  same content as the left's Y becomes "rename X to Y on the right" (a move or
  rename on the left is replayed instead of transferred again); a renamed
  folder becomes a single folder rename;
* a file whose content is identical on both sides but whose modification time
  or permissions differ only gets its attributes updated.

Names are paired by :mod:`mysync.names`, so the sides may spell them
differently in case or Unicode form; a name changed in that way only is
renamed on the other side.
"""

from __future__ import annotations

import bisect
import posixpath
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from mysync import fmt
from mysync import selection as selection_mod
from mysync import state as state_mod
from mysync.backends.base import HASH_ALGOS, TMP_SUFFIX, Backend, link_inside
from mysync.config import JobConfig
from mysync.filters import PathFilter
from mysync.lock import JobLock
from mysync.model import DIR, FILE, LINK, Action, Category, Change, Entry, SyncError
from mysync.names import NameMatcher, PathMap, detect_case_insensitive, mapped, pair_names

# progress(stage_text)
StageFn = Callable[[str], None]

_MOVE = {"right": Action.MOVE_R, "left": Action.MOVE_L}
_COPY_TO = {"right": Action.COPY_LR, "left": Action.COPY_RL}
_DELETE_ON = {"right": Action.DELETE_R, "left": Action.DELETE_L}
_EVICT_ON = {"right": Action.EVICT_R, "left": Action.EVICT_L}
_OTHER = {"left": "right", "right": "left"}


@dataclass
class Item:
    path: str
    left: Entry | None
    right: Entry | None
    category: Category
    lchange: Change = Change.UNKNOWN
    rchange: Change = Change.UNKNOWN
    action: Action = Action.NONE
    default_action: Action = Action.NONE
    conflict: bool = False
    reason: str = ""
    locked: bool = False  # action cannot be changed (e.g. file vs. folder)
    covered: bool = False  # handled by deleting an ancestor folder
    content_equal: bool = False  # same content on both sides; only attributes differ
    move_src: str | None = None  # this path can be created by renaming move_src
    move_dst: str | None = None  # this item can be renamed to move_dst
    # after the rename, copy time/permissions: "dst" onto the renamed file,
    # "src" from it back onto this item's file on the other side
    move_attrs: str | None = None
    moved_away: bool = False  # source of an active move: disappears through the rename
    via_move: bool = False  # handled by the rename of an ancestor folder
    # selective sync: the side that does not keep this item ("left"/"right")
    outside: str | None = None
    evict_after: bool = False  # outside the selection: free the local copy once uploaded
    # the name differs only in case/Unicode form: the side that takes the other's spelling
    fix_name: str | None = None
    # left out of this round by the user (TUI): skipped, hidden, the old state kept
    ignored: bool = False
    action_before_ignore: Action | None = None
    held: bool = False  # folder not deleted because it holds ignored items
    status: str = ""  # after sync: "done", "failed", "cancelled", "changed", "kept"
    error: str = ""

    @property
    def is_dir(self) -> bool:
        e = self.left or self.right
        return e is not None and e.kind == DIR

    @property
    def depth(self) -> int:
        return self.path.count("/")

    @property
    def transfer_size(self) -> int:
        if self.via_move:
            return 0
        if self.action == Action.COPY_LR and self.left and self.left.kind == FILE:
            return self.left.size
        if self.action == Action.COPY_RL and self.right and self.right.kind == FILE:
            return self.right.size
        return 0

    def side_entry(self, side: str) -> Entry | None:
        return self.left if side == "left" else self.right

    @property
    def name_fix(self) -> str | None:
        """The side renamed to the other's spelling, unless the item is skipped,
        deleted or freed on a side anyway."""
        if (self.fix_name is None or self.action == Action.SKIP or self.action.is_delete
                or self.action.is_evict or self.via_move or self.moved_away):
            return None
        return self.fix_name

    def allowed_actions(self) -> list[Action]:
        if self.via_move:
            return [self.action]
        if self.locked:
            return [Action.SKIP]
        acts = []
        if self.left:
            acts.append(Action.COPY_LR)
        if self.right:
            acts.append(Action.COPY_RL)
        if self.move_src:
            acts.append(Action.MOVE_R if self.left else Action.MOVE_L)
        if self.content_equal and self.left and self.right:
            acts += [Action.ATTR_LR, Action.ATTR_RL]
        if self.left:
            acts.append(Action.DELETE_L)
        if self.right:
            acts.append(Action.DELETE_R)
        if self.outside and self.side_entry(self.outside) and self.side_entry(_OTHER[self.outside]):
            acts.append(_EVICT_ON[self.outside])
        acts.append(Action.SKIP)
        return acts


@dataclass
class Comparison:
    job: JobConfig
    items: list[Item]
    state: state_mod.PairState | None
    state_message: str
    left_root_exists: bool
    right_root_exists: bool
    left_count: int = 0
    right_count: int = 0
    hash_algo: str = ""
    warnings: list[str] = field(default_factory=list)
    # how each side spells the (shared) item paths
    maps: dict[str, PathMap] = field(default_factory=lambda: {"left": PathMap(), "right": PathMap()})
    free: dict[str, int | None] = field(default_factory=dict)  # free bytes per side when compared
    by_path: dict[str, Item] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_path = {i.path: i for i in self.items}
        self.paths = [i.path for i in self.items]  # sorted

    def descendants(self, item: Item) -> list[Item]:
        """Everything below a folder.

        They form one contiguous block of the sorted list, though not
        necessarily right after the folder ("a-b" sorts between "a" and "a/x").
        """
        prefix = item.path + "/"
        out = []
        for n in range(bisect.bisect_left(self.paths, prefix), len(self.paths)):
            if not self.paths[n].startswith(prefix):
                break
            out.append(self.items[n])
        return out

    # -- summaries --------------------------------------------------------
    def pending(self) -> list[Item]:
        return [i for i in self.items if (i.action not in (Action.NONE, Action.SKIP) or i.name_fix)
                and not (i.covered or i.via_move or i.moved_away)]

    def counts(self) -> dict[str, int]:
        c = {a.value: 0 for a in Action}
        for i in self.items:
            if not (i.via_move or i.moved_away):
                c[i.action.value] += 1
        c["conflict"] = sum(1 for i in self.items if i.conflict and i.action == Action.SKIP
                            and not i.ignored)
        c["ignored"] = sum(1 for i in self.items if i.ignored)
        c["names"] = sum(1 for i in self.items if i.name_fix)
        return c

    def transfer_bytes(self) -> int:
        return sum(i.transfer_size for i in self.items)

    def freed_bytes(self) -> int:
        """Space released on the selective side by freeing local copies."""
        total = 0
        for i in self.items:
            if i.outside and not i.ignored and (i.action.is_evict or i.evict_after):
                e = i.side_entry(i.outside)
                if e and e.kind == FILE:
                    total += e.size
        return total

    def deletion_ratio(self) -> tuple[float, int, int]:
        """Worst (fraction, deleted, total) of either side's items to be deleted."""
        worst = (0.0, 0, 0)
        for act, total in ((Action.DELETE_L, self.left_count), (Action.DELETE_R, self.right_count)):
            n = sum(1 for i in self.items if i.action == act and not i.moved_away)
            if total and n / total > worst[0]:
                worst = (n / total, n, total)
        return worst

    def space_needed(self) -> dict[str, int]:
        """Bytes each side gains: copies to it, less what is deleted or overwritten
        there when deleted files do not go to the trash (which is on the same location)."""
        need = {"left": 0, "right": 0}
        purge = self.job.deletion == "delete"
        for i in self.items:
            if i.via_move or i.moved_away:
                continue
            if i.action.is_copy:
                side = "right" if i.action == Action.COPY_LR else "left"
                need[side] += i.transfer_size
            elif i.action.is_delete:
                side = "left" if i.action == Action.DELETE_L else "right"
            else:
                continue
            old = i.side_entry(side)
            if purge and old is not None and old.kind == FILE:
                need[side] -= old.size
        return need

    def space_problem(self) -> str | None:
        problems = []
        for side, need in self.space_needed().items():
            free = self.free.get(side)
            if free is not None and need > free:
                problems.append(f"the {side} side needs {fmt.size(need)} "
                                f"but has only {fmt.size(free)} free")
        return "; ".join(problems) or None

    def safety_problem(self) -> str | None:
        """Explain why the sync looks dangerous or cannot work, or None."""
        problems = []
        limit = self.job.max_delete_percent
        frac, n, total = self.deletion_ratio()
        if limit > 0 and n > 10 and frac * 100 > limit:
            problems.append(f"{n} of {total} items ({frac:.0%}) on one side would be deleted, "
                            f"more than max_delete_percent = {limit:g}")
        space = self.space_problem()
        if space:
            problems.append(f"not enough space: {space}")
        return "; ".join(problems) or None

    # -- user overrides ---------------------------------------------------
    def set_action(self, item: Item, action: Action) -> bool:
        ok = self._set_action(item, action)
        if ok:
            finalize(self)
        return ok

    def set_actions(self, changes: list[tuple[Item, Action]]) -> tuple[int, int]:
        """Apply several choices at once; returns (applied, not possible)."""
        done = failed = 0
        for item, action in changes:
            if self._set_action(item, action):
                done += 1
            else:
                failed += 1
        finalize(self)
        return done, failed

    def _set_action(self, item: Item, action: Action) -> bool:
        if item.via_move or item.ignored:
            return False
        if action not in item.allowed_actions() and action != item.default_action:
            return False
        if action.is_move and item.move_src:
            src = self.by_path[item.move_src]
            src.action = _DELETE_ON["right" if action == Action.MOVE_R else "left"]
        item.action = action
        if item.is_dir and action.is_delete:
            # deleting a folder deletes what is inside it on that side
            side = "left" if action == Action.DELETE_L else "right"
            for child in self.descendants(item):
                if (child.side_entry(side) and not child.locked and not child.via_move
                        and not child.ignored):
                    child.action = action
        return True

    def set_ignored(self, items: list[Item], on: bool) -> tuple[int, int]:
        """Leave items out of this round (or take them back): nothing is done
        to them and their last synchronized state is kept, as if they had not
        changed.  A folder takes its content along, a rename both its ends.
        Returns (changed, not possible)."""
        group: dict[str, Item] = {}
        failed = 0

        def add(item: Item) -> None:
            for i in (item, *(self.descendants(item) if item.is_dir else ())):
                group[i.path] = i

        for item in items:
            if item.via_move:  # cannot be separated from its folder's rename
                failed += on
                continue
            add(item)
            for end in (item.move_src, item.move_dst):
                other = self.by_path.get(end or "")
                if other and (item.action.is_move or item.moved_away
                              or (not on and other.ignored and item.ignored)):
                    add(other)
        changed = [i for i in group.values() if i.ignored != on]
        for i in changed:
            if on:
                if not i.held:
                    i.action_before_ignore = i.action
                i.ignored, i.held, i.action = True, False, Action.SKIP
            else:
                i.ignored, i.action = False, i.action_before_ignore or i.default_action
                i.action_before_ignore = None
        if not on:
            for i in self.items:  # folders kept for ignored content: finalize decides again
                if i.held:
                    i.action, i.held, i.action_before_ignore = i.action_before_ignore, False, None
            # a rename that fell back to a copy while its source was left out
            for i in changed:
                dst = self.by_path.get(i.move_dst or "")
                if dst and dst.default_action.is_move and dst.action.is_copy and not dst.ignored:
                    dst.action = dst.default_action
        finalize(self)
        return len(changed), failed

    def only(self, roots: list[str]) -> None:
        """Leave everything outside these paths untouched (e.g. freeing one folder)."""
        def inside(path: str) -> bool:
            return any(not r or path == r or path.startswith(r + "/") for r in roots)
        for i in self.items:
            if not inside(i.path):
                i.action = i.default_action = Action.NONE
                i.evict_after = False
        finalize(self)

    def reset_actions(self) -> None:
        for i in self.items:
            i.action = i.default_action
            i.ignored, i.held, i.action_before_ignore = False, False, None
        finalize(self)


def toward(item: Item, side: str) -> Action | None:
    """The action that makes ``side`` like the other side ("sync to the
    right/left"), or None when there is nothing to do."""
    if item.category == Category.EQUAL or item.via_move or item.moved_away:
        return None
    planned = item.default_action
    if (planned.changes_right if side == "right" else planned.changes_left) and not planned.is_evict:
        return planned  # keeps renames and time-only updates
    if item.side_entry(_OTHER[side]) is None:
        return _DELETE_ON[side]
    if item.content_equal and item.side_entry(side) is not None:
        return Action.ATTR_LR if side == "right" else Action.ATTR_RL
    return _COPY_TO[side]


# --------------------------------------------------------------------------
# classification


def same_time(job: JobConfig, a: float, b: float) -> bool:
    """Equal modification times: within the tolerance, or apart by an ignored
    whole shift (FAT file systems store local time, so DST moves them by an hour)."""
    d = abs(a - b)
    if d <= job.time_tolerance:
        return True
    return any(abs(d - hours * 3600) <= job.time_tolerance for hours in job.ignore_time_shift)


def _content_equal(job: JobConfig, l: Entry, r: Entry, hashes: tuple | None) -> bool:
    if l.kind != r.kind:
        return False
    if l.kind == DIR:
        return True
    if l.kind == LINK:
        return l.target == r.target
    if l.size != r.size:
        return False
    if job.compare == "size":
        return True
    if job.compare == "content":
        return bool(hashes and hashes[0] is not None and hashes[0] == hashes[1])
    return same_time(job, l.mtime, r.mtime)


def _attrs_equal(job: JobConfig, l: Entry, r: Entry) -> bool:
    if not job.sync_permissions or l.kind != FILE or l.mode is None or r.mode is None:
        return True
    return l.mode == r.mode


def _category(job: JobConfig, l: Entry | None, r: Entry | None, equal: bool,
              content_equal: bool) -> Category:
    if r is None:
        return Category.LEFT_ONLY
    if l is None:
        return Category.RIGHT_ONLY
    if l.kind != r.kind:
        return Category.CONFLICT
    if equal:
        return Category.EQUAL
    if content_equal:
        return Category.ATTRIBUTES
    if l.mtime > r.mtime + job.time_tolerance:
        return Category.LEFT_NEWER
    if r.mtime > l.mtime + job.time_tolerance:
        return Category.RIGHT_NEWER
    return Category.DIFFERENT


def _change(job: JobConfig, cur: Entry | None, old: Entry | None, known: bool) -> Change:
    if not known:
        return Change.UNKNOWN
    if old is None:
        return Change.NEW if cur else Change.ABSENT
    if cur is None:
        return Change.DELETED
    if cur.kind != old.kind:
        return Change.MODIFIED
    if cur.kind == LINK and cur.target != old.target:
        return Change.MODIFIED
    if cur.kind == FILE:
        if cur.size != old.size:
            return Change.MODIFIED
        d = abs(cur.mtime - old.mtime)
        # an ignored time shift is no change, but within the tolerance it is
        shifted = job.ignore_time_shift and d > job.time_tolerance and same_time(job, cur.mtime, old.mtime)
        if d > 1e-6 and not shifted:
            return Change.MODIFIED
        if (job.sync_permissions and cur.mode is not None and old.mode is not None
                and cur.mode != old.mode):
            return Change.MODIFIED
    return Change.UNCHANGED


def _resolve_conflict(job: JobConfig, item: Item) -> Action:
    l, r = item.left, item.right
    policy = job.conflict
    if policy == "left":
        return Action.DELETE_R if l is None else Action.COPY_LR
    if policy == "right":
        return Action.DELETE_L if r is None else Action.COPY_RL
    if policy == "newer":
        if l is None:
            return Action.COPY_RL  # deleted vs. modified: keep the modified one
        if r is None:
            return Action.COPY_LR
        if l.mtime > r.mtime:
            return Action.COPY_LR
        if r.mtime > l.mtime:
            return Action.COPY_RL
    return Action.SKIP


_CHANGE_WORD = {
    Change.NEW: "created", Change.MODIFIED: "modified", Change.DELETED: "deleted",
    Change.UNCHANGED: "unchanged", Change.ABSENT: "absent", Change.UNKNOWN: "unknown",
}


def _decide_two_way(job: JobConfig, item: Item, known: bool) -> None:
    l, r = item.left, item.right
    if not known:
        if r is None:
            item.action, item.reason = Action.COPY_LR, "only on left"
        elif l is None:
            item.action, item.reason = Action.COPY_RL, "only on right"
        else:
            item.conflict, item.reason = True, "differs and there is no previous state"
        return
    lchanged = item.lchange in (Change.NEW, Change.MODIFIED, Change.DELETED)
    rchanged = item.rchange in (Change.NEW, Change.MODIFIED, Change.DELETED)
    if lchanged and not rchanged:
        item.action = Action.DELETE_R if l is None else Action.COPY_LR
        item.reason = f"{_CHANGE_WORD[item.lchange]} on left"
    elif rchanged and not lchanged:
        item.action = Action.DELETE_L if r is None else Action.COPY_RL
        item.reason = f"{_CHANGE_WORD[item.rchange]} on right"
    else:
        item.conflict = True
        if lchanged and rchanged:
            item.reason = (f"{_CHANGE_WORD[item.lchange]} on left and "
                           f"{_CHANGE_WORD[item.rchange]} on right")
        else:
            item.reason = "differs although neither side changed since the last sync"


def _decide(job: JobConfig, item: Item, known: bool, equal: bool) -> None:
    l, r = item.left, item.right
    if equal:
        item.action = Action.NONE
        return
    if l is not None and r is not None and l.kind != r.kind:
        item.conflict, item.locked = True, True
        item.reason = "a file on one side and a folder on the other - resolve manually"
        item.action = Action.SKIP
        return
    if job.mode == "mirror":
        item.action = Action.DELETE_R if l is None else Action.COPY_LR
        item.reason = "mirror left to right"
    elif job.mode == "update":
        if l is None:
            item.action, item.reason = Action.NONE, "only on right (update mode keeps it)"
        elif known and item.lchange == Change.UNCHANGED:
            item.action, item.reason = Action.NONE, f"{_CHANGE_WORD[item.rchange]} on right, left unchanged"
        elif item.category == Category.RIGHT_NEWER:
            item.conflict, item.reason = True, "right side is newer"
        else:
            item.action, item.reason = Action.COPY_LR, "new or updated on left"
    else:
        _decide_two_way(job, item, known)
    if item.conflict:
        item.action = _resolve_conflict(job, item)


def _decide_name(job: JobConfig, item: Item, pair: state_mod.PairState | None,
                 maps: dict[str, PathMap]) -> None:
    """The sides spell the name differently: follow the side that renamed it
    since the last sync (mirror and update follow the left)."""
    names = {side: maps[side].name(item.path) for side in ("left", "right")}
    old = pair.names.get(item.path) if pair else None
    renamed = {side: old is not None and names[side] != posixpath.basename(old[n])
               for n, side in enumerate(("left", "right"))}
    side = None
    if job.mode != "two-way":
        if not (renamed["right"] and not renamed["left"]) or job.mode == "mirror":
            side = "right"
    elif old is not None:
        if renamed["left"] != renamed["right"]:
            side = "right" if renamed["left"] else "left"
    elif item.action.is_copy:
        side = "right" if item.action == Action.COPY_LR else "left"
    note = f"named {names['left']!r} on the left, {names['right']!r} on the right"
    if side is not None:
        item.fix_name = side
        note = f"renamed to {names[_OTHER[side]]!r} on the {side}"
    item.reason = f"{item.reason}; {note}" if item.reason else note


def _outside_selection(item: Item, partial: str) -> None:
    """Selective sync: never download or delete on the full side here; free
    what the selective side still holds once the full side has it."""
    full = _OTHER[partial]
    item.outside = partial
    if item.side_entry(partial) is None:
        item.action, item.conflict = Action.NONE, False
        item.reason = f"not selected (kept only on the {full})"
        return
    if item.conflict:
        item.reason += "; not selected - resolve this before the copy can be freed"
        return
    if item.action in (_COPY_TO[full], _DELETE_ON[partial]):
        if item.action == _COPY_TO[full]:
            item.evict_after = True
            item.reason += f"; not selected: freed on the {partial} afterwards"
        return
    if item.side_entry(full) is None:
        return
    newer = item.action == _COPY_TO[partial]
    item.action = _EVICT_ON[partial]
    item.reason = f"not selected: free the copy on the {partial}"
    if newer:
        item.reason += f" (the {full} has a newer version)"


# --------------------------------------------------------------------------
# plan consistency


def finalize(cmp: Comparison) -> None:
    """Make the plan consistent.

    * A folder cannot be deleted on a side where something inside it stays;
      it is then re-created on the other side instead.
    * Items below a folder that cannot be synchronized are skipped.
    * A rename is only possible while its source is still planned to vanish;
      otherwise it falls back to a copy.
    * Items inside a folder that is deleted or renamed as a whole are marked
      as handled by that folder.
    """
    items = cmp.items
    blocked: list[str] = [i.path + "/" for i in items if i.locked]
    keep_left: set[str] = set()
    keep_right: set[str] = set()
    # folders holding ignored items: left as they are rather than re-created
    held_left: set[str] = set()
    held_right: set[str] = set()
    for item in sorted(items, key=lambda i: i.depth, reverse=True):
        held = item.ignored
        item.covered = False
        if blocked and not item.locked and any(item.path.startswith(b) for b in blocked):
            item.action = Action.SKIP
            item.reason = "inside a folder that cannot be synchronized"
        if item.is_dir:
            if item.action == Action.DELETE_R and item.path in keep_right:
                item.action = Action.COPY_RL if item.left is None else Action.NONE
                item.reason = "folder still has content on the right"
            elif item.action == Action.DELETE_L and item.path in keep_left:
                item.action = Action.COPY_LR if item.right is None else Action.NONE
                item.reason = "folder still has content on the left"
            elif ((item.action == Action.DELETE_R and item.path in held_right)
                  or (item.action == Action.DELETE_L and item.path in held_left)):
                item.action_before_ignore, item.action = item.action, Action.NONE
                item.held = held = True
        # a rename has the same effect on existence as the copy it replaces
        exists_l = ((item.left is not None and item.action not in (Action.DELETE_L, Action.EVICT_L))
                    or item.action in (Action.COPY_RL, Action.MOVE_L))
        exists_r = ((item.right is not None and item.action not in (Action.DELETE_R, Action.EVICT_R))
                    or item.action in (Action.COPY_LR, Action.MOVE_R))
        parent = item.path.rsplit("/", 1)[0] if "/" in item.path else None
        if parent is not None:
            if exists_l:
                (held_left if held else keep_left).add(parent)
            if exists_r:
                (held_right if held else keep_right).add(parent)

    for item in items:
        item.moved_away = item.via_move = False
    for item in items:
        if not item.action.is_move:
            continue
        side = "right" if item.action == Action.MOVE_R else "left"
        src = cmp.by_path.get(item.move_src or "")
        if src is None or src.action != _DELETE_ON[side]:
            item.action = _COPY_TO[side]
            continue
        src.moved_away = True
    for item in items:
        if item.action.is_move and item.is_dir and not item.via_move:
            src = cmp.by_path[item.move_src]
            for d in cmp.descendants(item):
                d.via_move = True
            for d in cmp.descendants(src):
                d.via_move = d.moved_away = True

    for item in items:  # parents come before their content
        if item.is_dir and item.action.is_delete and not item.covered:
            for d in cmp.descendants(item):
                d.covered = d.action == item.action


# --------------------------------------------------------------------------
# optimization: renames and attribute-only updates


def _pick_hash(left: Backend, right: Backend) -> tuple[str, bool]:
    """Best checksum both sides can compute where the data lives."""
    common = left.hash_algos() & right.hash_algos()
    for algo in HASH_ALGOS:
        if algo in common:
            return algo, True
    return "sha256", False


def _hash_both(left: Backend, right: Backend, lpaths: list[str], rpaths: list[str],
               algo: str, stage: StageFn) -> tuple[dict[str, str], dict[str, str]]:
    done = {"left": 0, "right": 0}
    total = len(lpaths) + len(rpaths)

    def run(side: str, backend: Backend, paths: list[str]) -> dict[str, str]:
        def prog(n: int) -> None:
            done[side] = n
            stage(f"checksums ({algo}): {done['left'] + done['right']}/{total}")
        return backend.hash_many(paths, algo, prog) if paths else {}

    with ThreadPoolExecutor(2) as pool:
        lf = pool.submit(run, "left", left, lpaths)
        rf = pool.submit(run, "right", right, rpaths)
        return lf.result(), rf.result()


def _pick_move_source(cmp: Comparison, add: Item, cands: list[Item], side: str) -> Item:
    """Several equal files could be the origin: prefer the same file id, then name."""
    src_side = 0 if side == "right" else 1
    entry = add.side_entry("left" if side == "right" else "right")
    name = posixpath.basename(add.path)

    def score(d: Item) -> tuple:
        old = cmp.state.entries.get(d.path) if cmp.state else None
        same_id = bool(old and entry.fid and old[src_side].fid == entry.fid)
        other = d.side_entry(side)
        return (not same_id, posixpath.basename(d.path) != name, abs(other.mtime - entry.mtime), d.path)
    return min(cands, key=score)


def _collapse_folder_moves(cmp: Comparison, side: str) -> None:
    """Turn a folder whose whole content was renamed into one folder rename."""
    move, copy, delete = _MOVE[side], _COPY_TO[side], _DELETE_ON[side]
    done: list[str] = []
    for n in cmp.items:
        if (not n.is_dir or n.action != copy or n.side_entry(side) is not None
                or any(n.path.startswith(p) for p in done)):
            continue
        desc = cmp.descendants(n)
        first = next((d for d in desc if d.action == move and d.move_src), None)
        if first is None:
            continue
        rest = first.path[len(n.path):]  # "/sub/file"
        if not first.move_src.endswith(rest) or len(first.move_src) == len(rest):
            continue
        o_path = first.move_src[: -len(rest)]
        o = cmp.by_path.get(o_path)
        if (o is None or not o.is_dir or o.action != delete or o.side_entry(side) is None
                or n.path.startswith(o_path + "/") or o_path.startswith(n.path + "/")):
            continue
        if len(cmp.descendants(o)) != len(desc):
            continue
        ok = True
        for d in desc:
            counterpart = cmp.by_path.get(o_path + d.path[len(n.path):])
            if counterpart is None or counterpart.action != delete:
                ok = False
            elif d.is_dir:
                ok = d.action == copy and counterpart.is_dir and d.side_entry(side) is None
            else:
                ok = d.action == move and d.move_src == counterpart.path
            if not ok:
                break
        if not ok:
            continue
        n.action, n.move_src, o.move_dst = move, o_path, n.path
        n.reason = f"folder renamed from {o_path}"
        o.reason = f"folder renamed to {n.path}"
        done.append(n.path + "/")


def _optimize(cmp: Comparison, left: Backend, right: Backend, algo: str, native: bool,
              stage: StageFn) -> None:
    job = cmp.job
    items = cmp.items
    verify = job.verify_before_copy and native and job.compare != "content"
    must_hash_moves = verify or job.compare == "content"
    need = {"left": set(), "right": set()}

    attr_cands: list[Item] = []
    if verify:
        for i in items:
            l, r = i.left, i.right
            # empty files are always just copied: there is nothing to verify
            if (l and r and l.kind == r.kind == FILE and l.size == r.size and l.size > 0
                    and not i.content_equal and not i.locked and (i.action.is_copy or i.conflict)):
                attr_cands.append(i)
                need["left"].add(i.path)
                need["right"].add(i.path)

    # (item to create, possible sources, side where the rename happens)
    # With checksums, a rename is recognized by content alone, so it may come
    # with other changes (time, permissions) that are applied afterwards.
    # Without them only exact size + time matches are trusted.
    move_cands: list[tuple[Item, list[Item], str]] = []
    if job.detect_moves:
        for side, other in (("right", "left"), ("left", "right")):
            by_size: dict[int, list[Item]] = {}
            for d in items:
                e = d.side_entry(side)
                if not e or e.kind != FILE or e.size == 0 or d.locked:
                    continue
                # deleted on the other side, only touched here: still a rename
                touched = (must_hash_moves and d.conflict and d.action == Action.SKIP
                           and d.side_entry(other) is None)
                if d.action == _DELETE_ON[side] or touched:
                    by_size.setdefault(e.size, []).append(d)
            if not by_size:
                continue
            for add in items:
                a = add.side_entry(other)
                if (add.action != _COPY_TO[side] or a is None or a.kind != FILE
                        or add.side_entry(side) is not None or a.size not in by_size):
                    continue
                cands = []
                for d in by_size[a.size]:
                    b = d.side_entry(side)
                    if not must_hash_moves:
                        if not same_time(job, b.mtime, a.mtime):
                            continue
                        if job.sync_permissions and None not in (a.mode, b.mode) and a.mode != b.mode:
                            continue
                    cands.append(d)
                if cands:
                    move_cands.append((add, cands, side))
                    if must_hash_moves:
                        need[other].add(add.path)
                        need[side].update(d.path for d in cands)

    hashes = {"left": {}, "right": {}}
    if need["left"] or need["right"]:
        hashes["left"], hashes["right"] = _hash_both(
            left, right, sorted(need["left"]), sorted(need["right"]), algo, stage)

    # identical content: only attributes need to be updated
    for i in attr_cands:
        h = hashes["left"].get(i.path)
        if h and h == hashes["right"].get(i.path):
            i.content_equal = True
            i.category = Category.ATTRIBUTES
    for i in items:
        if not i.content_equal or i.locked or not (i.left and i.right):
            continue
        if i.action.is_copy:
            i.action = Action.ATTR_LR if i.action == Action.COPY_LR else Action.ATTR_RL
            i.reason = f"{i.reason}; content identical" if i.reason else "content identical"
            i.conflict = False
        elif i.conflict and i.action == Action.SKIP and not same_time(job, i.left.mtime, i.right.mtime):
            i.action = Action.ATTR_LR if i.left.mtime > i.right.mtime else Action.ATTR_RL
            i.reason = "content identical; newer modification time wins"
            i.conflict = False

    # renames
    for side in ("right", "left"):
        other = "left" if side == "right" else "right"
        used: set[str] = set()
        for add, cands, s in move_cands:
            if s != side:
                continue
            valid = [d for d in cands if d.path not in used]
            if must_hash_moves:
                h = hashes[other].get(add.path)
                valid = [d for d in valid if h and hashes[side].get(d.path) == h]
            if not valid:
                continue
            src = _pick_move_source(cmp, add, valid, side)
            used.add(src.path)
            if src.action != _DELETE_ON[side]:  # the touched-vs-deleted conflict is solved
                src.action, src.conflict = _DELETE_ON[side], False
            add.action, add.move_src, src.move_dst = _MOVE[side], src.path, add.path
            add.reason = f"moved from {src.path}"
            src.reason = f"moved to {add.path}"
            a, b = add.side_entry(other), src.side_entry(side)
            modes_differ = job.sync_permissions and None not in (a.mode, b.mode) and a.mode != b.mode
            if not same_time(job, a.mtime, b.mtime) or modes_differ:
                add.move_attrs = "dst" if a.mtime >= b.mtime else "src"
                add.reason += "; time/permissions updated"
        _collapse_folder_moves(cmp, side)


# --------------------------------------------------------------------------


def builtin_excludes(job: JobConfig) -> list[str]:
    pats = ["/.mysync/", f"*{TMP_SUFFIX}"]
    for loc in (job.left, job.right):
        pats.append(f"/{loc.trash_dir}/")
        if loc.state_in_location:
            pats.append(f"/{loc.state_dir}/")
    return pats


def compare(job: JobConfig, left: Backend, right: Backend, stage: StageFn | None = None,
            selection: selection_mod.Selection | None = None) -> Comparison:
    """Scan both locations and plan the synchronization (changes nothing).

    ``selection`` overrides the saved selection of a selective job.
    """
    with JobLock(job, "compare"):
        return _compare(job, left, right, stage or (lambda _msg: None), selection)


def _compare(job: JobConfig, left: Backend, right: Backend, stage: StageFn,
             sel: selection_mod.Selection | None) -> Comparison:
    flt = PathFilter(job.include, job.exclude + builtin_excludes(job))

    stage("checking locations")
    lexists, rexists = left.root_exists(), right.root_exists()
    stage("loading synchronization state")
    info = state_mod.load(job, left, right)
    pair = info.state
    if pair and pair.entries:
        for side, exists in (("left", lexists), ("right", rexists)):
            if not exists:
                raise SyncError(f"the {side} location does not exist although it was synchronized "
                                f"before - is it mounted/available? ({getattr(job, side).url})")

    counts = {"left": 0, "right": 0}

    def scan(side: str, backend: Backend, exists: bool, excluded=flt.excluded) -> dict[str, Entry]:
        if not exists:
            return {}

        def prog(n: int) -> None:
            counts[side] = n
            stage(f"scanning: {counts['left']} left, {counts['right']} right")
        return backend.scan(excluded, job.symlinks, prog)

    if not job.selective:
        sel = None
    elif sel is None:
        sel = selection_mod.load(job)
    if sel is not None and sel.is_everything:
        sel = None
    if sel is None:
        with ThreadPoolExecutor(2) as pool:
            lf = pool.submit(scan, "left", left, lexists)
            rf = pool.submit(scan, "right", right, rexists)
            lscan, rscan = lf.result(), rf.result()
    else:
        # the selective side first: outside the selection the full side is
        # only looked at where the selective side still has something
        partial = job.selective
        pscan = scan(partial, left if partial == "left" else right, lexists if partial == "left" else rexists)
        # case sensitivity is not known yet: ignoring it only means looking at more
        loose = NameMatcher(job.case_sensitive is True, job.unicode_normalization)
        pkeys = {loose.key(p) for p in pscan}

        def full_excluded(rel: str, is_dir: bool) -> bool:
            return flt.excluded(rel, is_dir) or (loose.key(rel) not in pkeys
                                                 and not sel.wanted(rel, is_dir))
        full = _OTHER[partial]
        fscan = scan(full, left if full == "left" else right, lexists if full == "left" else rexists,
                     full_excluded)
        lscan, rscan = (pscan, fscan) if partial == "left" else (fscan, pscan)

    warnings = []
    if job.symlinks == "links":
        for side, found in (("left", lscan), ("right", rscan)):
            for rel, e in list(found.items()):
                if e.kind == LINK and not link_inside(rel, e.target or ""):
                    del found[rel]
                    warnings.append(f"skipped link on the {side}: {rel} -> {e.target} "
                                    "(only relative links inside the synchronized folder are copied)")

    case_sensitive = job.case_sensitive
    if case_sensitive is None:
        case_sensitive = not (detect_case_insensitive(left, lscan)
                              or detect_case_insensitive(right, rscan))
    matcher = NameMatcher(case_sensitive, job.unicode_normalization)
    maps = {"left": PathMap(), "right": PathMap()}
    if not matcher.exact:
        lscan, rscan, maps["left"], maps["right"], shared, clashes = pair_names(
            lscan, rscan, matcher.key)
        warnings += clashes
        if pair:
            pair.join(matcher.key)
            pair.entries = {shared[k]: v for k, v in pair.entries.items() if k in shared}
            pair.names = {shared[k]: v for k, v in pair.names.items() if k in shared}
        left, right = mapped(left, maps["left"]), mapped(right, maps["right"])

    algo, native = _pick_hash(left, right)
    known = pair is not None
    old = pair.entries if pair else {}
    items: list[Item] = []
    for path in sorted(lscan.keys() | rscan.keys()):
        l, r = lscan.get(path), rscan.get(path)
        item = Item(path, l, r, Category.EQUAL)
        prev = old.get(path)
        item.lchange = _change(job, l, prev[0] if prev else None, known)
        item.rchange = _change(job, r, prev[1] if prev else None, known)
        items.append(item)

    hashes: dict[str, tuple] = {}
    if job.compare == "content":
        todo = []
        for item in items:
            l, r = item.left, item.right
            if not (l and r and l.kind == r.kind == FILE and l.size == r.size):
                continue
            if item.lchange == item.rchange == Change.UNCHANGED:
                # identical at the last sync and untouched since: no need to read them
                hashes[item.path] = ("same", "same")
            else:
                todo.append(item.path)
        if todo:
            lh, rh = _hash_both(left, right, todo, todo, algo, stage)
            for path in todo:
                hashes[path] = (lh.get(path), rh.get(path))

    for item in items:
        l, r = item.left, item.right
        if l is None or r is None:
            content_eq = equal = l is None and r is None
        else:
            content_eq = _content_equal(job, l, r, hashes.get(item.path))
            if (content_eq and job.compare == "time-size" and l.kind == FILE
                    and (item.lchange == Change.MODIFIED) != (item.rchange == Change.MODIFIED)):
                # the state says exactly one side changed: within the time tolerance
                # that is still a real edit (e.g. same size, saved right after a sync)
                content_eq = False
            equal = content_eq and _attrs_equal(job, l, r)
        item.content_equal = bool(content_eq and l and r and l.kind == FILE)
        item.category = _category(job, l, r, equal, content_eq)
        _decide(job, item, known, equal)
        if l and r and maps["left"].name(item.path) != maps["right"].name(item.path) and not item.locked:
            _decide_name(job, item, pair, maps)
        if sel is not None and not sel.wanted(maps[_OTHER[job.selective]].real(item.path), item.is_dir):
            _outside_selection(item, job.selective)

    stage("checking free space")
    free = {side: backend.free_space() for side, backend in (("left", left), ("right", right))}
    cmp = Comparison(job, items, pair, info.message, lexists, rexists, len(lscan), len(rscan), algo,
                     warnings, maps, free)
    finalize(cmp)
    _optimize(cmp, left, right, algo, native, stage)
    finalize(cmp)
    for item in items:
        item.default_action = item.action
    stage("comparison finished")
    return cmp
