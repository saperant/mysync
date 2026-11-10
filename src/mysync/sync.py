"""Executing a comparison.

Order: names that differ only in case or Unicode form are fixed, folders are
created, renames replayed, deletions run, attributes updated, files copied
and finally, with selective sync, local copies outside the selection freed
(only after the other side is verified to hold them).  Files (copies,
deletions, attribute updates) are processed by ``transfers`` parallel workers.

Every item is handled independently; a failure is recorded on the item and
the run continues.  Right before an item is modified, its current state is
compared with what the comparison saw; if it changed in the meantime the
item is left alone.  The new state is written at the end, even when the run
was cancelled or some items failed, so finished work is never redone and
unfinished work is retried next time.  Failed items are first tried again in
up to ``retries`` rounds within the same run.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from mysync import state as state_mod
from mysync.backends.base import Backend, block_digest, delta_block_size, parent, tmp_name
from mysync.compare import Comparison, Item, _hash_both, _pick_hash, finalize, same_time
from mysync.config import LocationConfig
from mysync.lock import JobLock
from mysync.model import DIR, FILE, LINK, Action, Category, ChangedError, Entry, SyncError
from mysync.names import MappedBackend, mapped

TRASH_FORMAT = "%Y-%m-%d_%H%M%S"
_STATE_ERROR = "could not save synchronization state"
_MAX_RANGE = 8 * 1024 * 1024  # largest piece sent in one delta record


class Cancelled(Exception):
    pass


class _Kept(Exception):
    """A folder outside the selection still holds something; it stays."""


class Reporter:
    """Receives progress information; subclass and override what you need.

    Methods may be called from several worker threads at once.
    """

    def __init__(self) -> None:
        self.cancel_event = threading.Event()

    def log(self, message: str) -> None:
        pass

    def item_start(self, item: Item, description: str) -> None:
        pass

    def item_done(self, item: Item) -> None:
        pass

    def transferred(self, nbytes: int) -> None:
        pass


@dataclass
class SyncResult:
    done: int = 0
    failed: int = 0
    changed: int = 0  # left alone because they changed after the comparison
    skipped: int = 0
    freed: int = 0  # bytes released by freeing local copies (selective sync)
    cancelled: bool = False
    bytes: int = 0
    errors: list[str] = field(default_factory=list)
    state_saved: bool = False
    seconds: float = 0.0


def describe(item: Item) -> str:
    entry = item.left or item.right
    kind = {DIR: "folder", LINK: "link"}.get(entry.kind if entry else FILE, "file")
    text = {
        Action.COPY_LR: f"copy {kind} to right",
        Action.COPY_RL: f"copy {kind} to left",
        Action.MOVE_R: f"rename {kind} on right from {item.move_src}",
        Action.MOVE_L: f"rename {kind} on left from {item.move_src}",
        Action.ATTR_LR: "update time/permissions on right",
        Action.ATTR_RL: "update time/permissions on left",
        Action.DELETE_L: f"delete {kind} on left",
        Action.DELETE_R: f"delete {kind} on right",
        Action.EVICT_L: f"free {kind} on left (kept on right)",
        Action.EVICT_R: f"free {kind} on right (kept on left)",
    }.get(item.action, item.action.value)
    if item.ignored:
        return "ignored this time (left as it is)"
    if item.held:
        return "kept: holds ignored items"
    if item.action.is_move and item.move_attrs:
        text += " and update time/permissions"
    return text


class _Side:
    def __init__(self, name: str, backend: MappedBackend, loc: LocationConfig, scanned_dirs: set[str]):
        self.name = name
        self.backend = backend
        self.loc = loc
        self.dirs = scanned_dirs  # folders known to exist

    def trash_path(self, rel: str, stamp: str) -> str:
        return f"{self.loc.trash_dir}/{stamp}/{rel}"


def _ranges(changed: list[int], bs: int, size: int) -> list[tuple[int, int]]:
    """Merge changed block numbers into (offset, length) ranges."""
    out: list[list[int]] = []
    for i in changed:
        off, length = i * bs, min(bs, size - i * bs)
        if out and out[-1][0] + out[-1][1] == off and out[-1][1] + length <= _MAX_RANGE:
            out[-1][1] += length
        else:
            out.append([off, length])
    return [(o, n) for o, n in out]


class Synchronizer:
    def __init__(self, cmp: Comparison, left: Backend, right: Backend, reporter: Reporter | None = None):
        self.cmp = cmp
        self.job = cmp.job
        self.reporter = reporter or Reporter()
        now = datetime.now()
        # unique per run, also when two runs start within the same second
        self.stamp = f"{now.strftime(TRASH_FORMAT)}.{now.microsecond:06d}"
        ldirs = {"", *(i.path for i in cmp.items if i.left and i.left.kind == DIR)}
        rdirs = {"", *(i.path for i in cmp.items if i.right and i.right.kind == DIR)}
        if not cmp.left_root_exists:
            ldirs.discard("")
        if not cmp.right_root_exists:
            rdirs.discard("")
        self.left = _Side("left", mapped(left, cmp.maps["left"]), self.job.left, ldirs)
        self.right = _Side("right", mapped(right, cmp.maps["right"]), self.job.right, rdirs)
        self.result = SyncResult()
        self.written: dict[str, tuple[Entry, Entry]] = {}
        self.names_fixed: set[str] = set()
        self.failed_move_sources: list[str] = []
        self.verified: dict[str, bool] = {}  # path -> same checksum on both sides (eviction)
        self._lock = threading.Lock()  # result counters, folder bookkeeping

    # -- helpers ----------------------------------------------------------
    def _side(self, name: str) -> _Side:
        return self.left if name == "left" else self.right

    def _check_cancel(self) -> None:
        if self.reporter.cancel_event.is_set():
            raise Cancelled()

    def _on_bytes(self, n: int) -> None:
        with self._lock:
            self.result.bytes += n
        self.reporter.transferred(n)
        self._check_cancel()

    def _ensure_dir(self, side: _Side, rel: str) -> None:
        with self._lock:
            missing = []
            while rel not in side.dirs:
                missing.append(rel)
                if not rel:
                    break
                rel = parent(rel)
            for path in reversed(missing):
                side.backend.mkdir(path)
                side.dirs.add(path)

    def _expect(self, side: _Side, rel: str, scanned: Entry | None) -> None:
        """Refuse to touch an item that changed since the comparison."""
        current = side.backend.stat(rel, self.job.symlinks)
        if scanned is None:
            if current is not None:
                raise ChangedError(f"appeared on the {side.name} after the comparison - compare again")
        elif not scanned.same_as(current):
            what = "disappeared" if current is None else "changed"
            raise ChangedError(f"{what} on the {side.name} after the comparison - compare again")

    def _discard(self, side: _Side, rel: str, is_dir: bool) -> None:
        """Delete or move to trash, depending on the job's deletion policy."""
        if self.job.deletion == "trash":
            side.backend.move(rel, side.trash_path(rel, self.stamp))
        else:
            side.backend.remove(rel, is_dir)

    # -- operations -------------------------------------------------------
    def _fix_name(self, item: Item) -> None:
        """Give one side the other's spelling of the name (case/Unicode form)."""
        side, other = self._side(item.name_fix), self._side(_other(item.name_fix))
        name = other.backend.map.name(item.path)
        self._expect(side, item.path, item.side_entry(side.name))
        raw, real = side.backend.raw, side.backend.map.real(item.path)
        target = f"{parent(real)}/{name}" if parent(real) else name
        # through a temporary name: storage that ignores case sees the target as existing
        tmp = tmp_name(target)
        raw.move(real, tmp)
        try:
            raw.move(tmp, target)
        except BaseException:
            try:
                raw.move(tmp, real)
            except OSError:
                pass
            raise
        side.backend.map.set_name(item.path, name)
        with self._lock:
            self.names_fixed.add(item.path)

    def _delete(self, item: Item) -> None:
        name = "left" if item.action == Action.DELETE_L else "right"
        side = self._side(name)
        if item.is_dir:
            prefix = item.path + "/"
            if any(p.startswith(prefix) for p in self.failed_move_sources):
                raise OSError("not deleted: something inside it could not be renamed")
        self._expect(side, item.path, item.side_entry(name))
        try:
            self._discard(side, item.path, item.is_dir)
        except FileNotFoundError:
            pass  # already gone: that is what we wanted

    def _copy(self, item: Item) -> None:
        if item.action == Action.COPY_LR:
            src, dst, sentry, dentry = self.left, self.right, item.left, item.right
        else:
            src, dst, sentry, dentry = self.right, self.left, item.right, item.left
        assert sentry is not None
        self._ensure_dir(dst, parent(item.path))
        if sentry.kind == DIR:
            new = dst.backend.mkdir(item.path)
            with self._lock:
                dst.dirs.add(item.path)
        else:
            self._expect(src, item.path, sentry)
            self._expect(dst, item.path, dentry)
            if sentry.kind == LINK:
                new = dst.backend.write_link(item.path, sentry.target or "")
            else:
                new = self._copy_file(item, src, dst, sentry, dentry)
        self.written[item.path] = (sentry, new) if item.action == Action.COPY_LR else (new, sentry)

    def _copy_file(self, item: Item, src: _Side, dst: _Side, sentry: Entry, dentry: Entry | None) -> Entry:
        mode = sentry.mode if self.job.sync_permissions else None
        keep_old = dentry is not None and self.job.deletion == "trash"
        if dentry is not None and dentry.kind == FILE:
            new = self._try_delta(item, src, dst, sentry, dentry, mode, keep_old)
            if new is not None:
                return new
        saved = None
        if keep_old:
            # keep the overwritten version
            saved = dst.trash_path(item.path, self.stamp)
            dst.backend.move(item.path, saved)
        try:
            with src.backend.open_read(item.path) as f:
                new = dst.backend.write_file(item.path, f, sentry.mtime, self._on_bytes, mode)
        except BaseException:
            if saved:
                try:
                    dst.backend.move(saved, item.path)  # put the old version back
                except OSError:
                    pass
            raise
        if new.size != sentry.size:
            raise OSError(f"size changed during copy ({sentry.size} -> {new.size} bytes); "
                          "the source was probably modified, it will be compared again next time")
        return new

    def _try_delta(self, item: Item, src: _Side, dst: _Side, sentry: Entry, dentry: Entry,
                   mode: int | None, keep_old: bool) -> Entry | None:
        """Send only the changed blocks; None when not possible or not worth it."""
        job = self.job
        if (not job.delta_transfer or sentry.size < job.delta_min_size or dentry.size == 0
                or not (src.backend.supports_delta() and dst.backend.supports_delta())
                or src.backend.loc.scheme == dst.backend.loc.scheme == "local"):
            return None
        bs = delta_block_size(sentry.size)
        try:
            with ThreadPoolExecutor(2) as pool:
                sf = pool.submit(src.backend.block_hashes, item.path, bs)
                df = pool.submit(dst.backend.block_hashes, item.path, bs)
                shashes, dhashes = sf.result(), df.result()
            changed = [i for i, h in enumerate(shashes) if i >= len(dhashes) or dhashes[i] != h]
            sent = sum(n for _, n in _ranges(changed, bs, sentry.size))
            if sent > sentry.size // 2:
                return None  # mostly different: a plain copy is simpler
            new = dst.backend.patch_file(
                item.path, src.backend.read_blocks(item.path, _ranges(changed, bs, sentry.size)),
                sentry.size, sentry.mtime, mode,
                dst.trash_path(item.path, self.stamp) if keep_old else None,
                (bs, block_digest(shashes)), self._on_bytes)
        except Cancelled:
            raise
        except (OSError, NotImplementedError) as e:
            self.reporter.log(f"WARNING delta transfer of {item.path} failed, copying it whole: {e}")
            return None
        self.reporter.log(f"delta transfer of {item.path}: sent {sent} of {sentry.size} bytes")
        return new

    def _move(self, item: Item) -> None:
        """Replay a rename: the content is already on the target side."""
        side, dst = ("left", self.right) if item.action == Action.MOVE_R else ("right", self.left)
        other = self._side(side)
        perms = self.job.sync_permissions
        try:
            self._expect(dst, item.move_src, self.cmp.by_path[item.move_src].side_entry(dst.name))
            dst.backend.move(item.move_src, item.path)
        except BaseException:
            self.failed_move_sources.append(item.move_src)
            raise
        while item.move_src in self.failed_move_sources:  # succeeded when retried
            self.failed_move_sources.remove(item.move_src)
        pairs = [(item, self.cmp.by_path[item.move_src])]
        if item.is_dir:
            for d in self.cmp.descendants(item):
                pairs.append((d, self.cmp.by_path[item.move_src + d.path[len(item.path):]]))
        for new, old in pairs:
            a = new.side_entry(side)
            b = old.side_entry(dst.name)
            if new.move_attrs == "dst":
                b = dst.backend.set_attrs(new.path, a.mtime, a.mode if perms else None)
            elif new.move_attrs == "src":
                a = other.backend.set_attrs(new.path, b.mtime, b.mode if perms else None)
            self.written[new.path] = (a, b) if side == "left" else (b, a)
            if new is not item:
                new.status = "done"
            old.status = "done"
        with self._lock:
            for new, _ in pairs:
                if new.is_dir:
                    dst.dirs.add(new.path)
            rel = parent(item.path)
            while rel and rel not in dst.dirs:
                dst.dirs.add(rel)
                rel = parent(rel)

    def _attrs(self, item: Item) -> None:
        if item.action == Action.ATTR_LR:
            src, dst, sentry, dentry = self.left, self.right, item.left, item.right
        else:
            src, dst, sentry, dentry = self.right, self.left, item.right, item.left
        self._expect(src, item.path, sentry)
        self._expect(dst, item.path, dentry)
        mode = sentry.mode if self.job.sync_permissions else None
        new = dst.backend.set_attrs(item.path, sentry.mtime, mode)
        self.written[item.path] = (sentry, new) if item.action == Action.ATTR_LR else (new, sentry)

    # -- selective sync ---------------------------------------------------
    def _to_free(self, items: list[Item]) -> list[Item]:
        out = []
        for i in items:
            if not i.outside or i.status in ("failed", "changed", "cancelled"):
                continue
            if i.action.is_evict and not (i.via_move or i.moved_away) and i.status == "":
                out.append(i)
            elif (i.evict_after and i.path in self.written and i.side_entry(i.outside)
                  and i.action.changes_left == (i.outside == "right")):
                out.append(i)  # uploaded (renamed, updated) in this run
        return out

    def _free_entries(self, item: Item) -> tuple[Entry, Entry | None]:
        """(selective side, full side) as they should be now."""
        if item.path in self.written:
            pair = self.written[item.path]
            return (pair[0], pair[1]) if item.outside == "left" else (pair[1], pair[0])
        return item.side_entry(item.outside), item.side_entry(_other(item.outside))

    def _verify_free(self, items: list[Item]) -> None:
        """Checksum the files to be freed on both sides, where the data lives."""
        algo, native = _pick_hash(self.left.backend, self.right.backend)
        files = [i.path for i in items if self._free_entries(i)[0].kind == FILE
                 and self._free_entries(i)[0].size > 0]
        if not native or not files:
            return
        self.reporter.log(f"verifying {len(files)} file(s) with {algo} before freeing them")
        lh, rh = _hash_both(self.left.backend, self.right.backend, files, files, algo,
                            lambda _msg: self._check_cancel())
        for path in files:
            self.verified[path] = lh.get(path) is not None and lh.get(path) == rh.get(path)

    def _free(self, item: Item) -> None:
        side, full = self._side(item.outside), self._side(_other(item.outside))
        mine, theirs = self._free_entries(item)
        if mine.kind == DIR:
            if side.backend.list_names(item.path):
                raise _Kept("folder kept: it still contains items that are not synchronized")
            side.backend.remove(item.path, True)
        else:
            self._expect(side, item.path, mine)
            current = full.backend.stat(item.path, self.job.symlinks)
            if current is None or current.kind != mine.kind:
                raise ChangedError(f"missing on the {full.name}; the local copy is kept")
            if mine.kind == LINK:
                same = current.target == mine.target
            elif current.size != mine.size:
                same = False
            elif item.path in self.verified:
                same = self.verified[item.path]
            elif mine.size == 0:
                same = True
            else:  # no checksums where the data lives: trust size and time
                same = same_time(self.job, current.mtime, mine.mtime)
            if same:
                side.backend.remove(item.path, False)
                with self._lock:
                    self.result.freed += mine.size
            elif item.action.is_evict and not item.content_equal and theirs is not None \
                    and not theirs.same_as(current):
                raise ChangedError(f"changed on the {full.name} after the comparison - compare again")
            elif item.action.is_evict and not item.content_equal:
                # an older version the full side has already replaced: as when overwriting
                self._discard(side, item.path, False)
            else:
                raise OSError(f"the copy on the {full.name} differs; the local copy is kept")
        with self._lock:
            self.written.pop(item.path, None)

    def _describe(self, item: Item, op: Callable[[Item], None]) -> str:
        if op == self._free:
            return f"free {item.path} on the {item.outside}"
        if op == self._fix_name:
            other = self._side(_other(item.name_fix))
            return f"rename on the {item.name_fix} to {other.backend.map.name(item.path)!r}"
        return describe(item)

    def _run_item(self, item: Item, op: Callable[[Item], None]) -> None:
        self._check_cancel()
        desc = self._describe(item, op)
        self.reporter.item_start(item, desc)
        try:
            op(item)
        except Cancelled:
            item.status = "cancelled"
            raise
        except _Kept as e:
            item.status, item.error = "kept", str(e)
            with self._lock:
                self.result.skipped += 1
            self.reporter.log(f"{item.path}: {e}")
        except ChangedError as e:
            item.status, item.error = "changed", str(e)
            with self._lock:
                self.result.changed += 1
            self.reporter.log(f"WARNING {item.path}: not synchronized: {e}")
        except (OSError, SyncError, EOFError) as e:
            item.status, item.error = "failed", str(e) or e.__class__.__name__
            with self._lock:
                self.result.failed += 1
                self.result.errors.append(f"{item.path}: {desc} failed: {item.error}")
            self.reporter.log(f"ERROR {item.path}: {desc}: {item.error}")
        else:
            if op != self._fix_name or item.action in (Action.NONE, Action.SKIP):
                item.status = "done"  # otherwise the item's action still follows
                with self._lock:
                    self.result.done += 1
            self.reporter.log(f"{desc}: {item.path}")
        self.reporter.item_done(item)

    def _run_all(self, items: list[Item], op: Callable[[Item], None], parallel: bool) -> None:
        """Run ``op`` for every item; raises Cancelled after the running ones finish."""
        if not parallel or self.job.transfers <= 1 or len(items) <= 1:
            for item in items:
                self._run_item(item, op)
            return
        cancelled = False
        with ThreadPoolExecutor(self.job.transfers) as pool:
            for future in [pool.submit(self._run_item, item, op) for item in items]:
                try:
                    future.result()
                except Cancelled:
                    cancelled = True
        if cancelled:
            raise Cancelled()

    def _purge_trash(self, side: _Side) -> None:
        days = self.job.trash_keep_days
        if self.job.deletion != "trash" or days <= 0:
            return
        limit = datetime.now() - timedelta(days=days)
        for name in side.backend.list_names(side.loc.trash_dir):
            try:
                when = datetime.strptime(name.split(".")[0], TRASH_FORMAT)
            except ValueError:
                continue
            if when < limit:
                try:
                    side.backend.remove(f"{side.loc.trash_dir}/{name}", True)
                    self.reporter.log(f"purged old trash {side.name}: {side.loc.trash_dir}/{name}")
                except OSError as e:
                    self.reporter.log(f"WARNING could not purge {side.loc.trash_dir}/{name}: {e}")

    def _new_state(self) -> tuple[dict[str, tuple[Entry, Entry]], dict[str, tuple[str, str]]]:
        """Entries to record, and the old spellings of items whose name fix is
        still to be done (so that the next run sees the same rename)."""
        old = self.cmp.state.entries if self.cmp.state else {}
        old_names = self.cmp.state.names if self.cmp.state else {}
        new: dict[str, tuple[Entry, Entry]] = {}
        names: dict[str, tuple[str, str]] = {}
        deleted_prefixes = [i.path + "/" for i in self.cmp.items
                            if i.is_dir and i.action.is_delete and i.status == "done"]
        for item in self.cmp.items:
            if item.fix_name and item.path not in self.names_fixed and item.status != "done":
                # skipped or failed: record the old spellings, so the rename is seen again
                if item.path in old and item.path in old_names:
                    new[item.path], names[item.path] = old[item.path], old_names[item.path]
                continue
            if item.path in self.written:  # written successfully (and not freed afterwards)
                new[item.path] = self.written[item.path]
                continue
            if item.status == "done":
                continue  # deleted or freed
            if item.covered and any(item.path.startswith(p) for p in deleted_prefixes):
                continue
            if item.category == Category.EQUAL and item.left and item.right:
                new[item.path] = (item.left, item.right)
            elif item.path in old:
                new[item.path] = old[item.path]  # not synchronized: keep the old baseline
        return new, names

    def _save_state(self) -> None:
        entries, names = self._new_state()

        def real(side: _Side, idx: int):
            return lambda path: names[path][idx] if path in names else side.backend.map.real(path)
        state_mod.save(self.job, self.left.backend.raw, self.right.backend.raw, entries,
                       (real(self.left, 0), real(self.right, 1)))

    # -- main -------------------------------------------------------------
    def run(self) -> SyncResult:
        with JobLock(self.job, "sync"):
            finalize(self.cmp)
            self.result.skipped = sum(1 for i in self.cmp.items if i.action == Action.SKIP)
            return self._run(self.cmp.items)

    def failed_items(self) -> list[Item]:
        return [i for i in self.cmp.items if i.status == "failed"]

    def retry_failed(self, left: Backend, right: Backend) -> SyncResult:
        """Run the failed items again (e.g. from the TUI after the run), with
        newly opened backends; returns the updated totals."""
        self.left.backend = mapped(left, self.cmp.maps["left"])
        self.right.backend = mapped(right, self.cmp.maps["right"])
        self.reporter.cancel_event.clear()
        self.result.cancelled = False
        with JobLock(self.job, "sync"):
            return self._run(self._reset(self.failed_items()))

    def _reset(self, items: list[Item]) -> list[Item]:
        prefixes = tuple(f"{i.path}: " for i in items)
        for item in items:
            item.status, item.error = "", ""
        with self._lock:
            self.result.failed -= len(items)
            self.result.errors = [e for e in self.result.errors if not e.startswith(prefixes)]
        return items

    def _execute(self, items: list[Item]) -> None:
        """One pass over ``items`` (in plan order) that are not finished yet."""
        todo = [i for i in items if i.status == "" and i.path not in self.written]
        self._run_all([i for i in todo if i.name_fix and i.path not in self.names_fixed],
                      self._fix_name, parallel=False)
        todo = [i for i in todo if i.status == ""]
        active = [i for i in todo if not (i.via_move or i.moved_away)]
        deletes = [i for i in active if i.action.is_delete and not i.covered]
        copies = [i for i in active if i.action.is_copy]  # already sorted: parents first
        # folders first, so renames and copies have a place to go; renames
        # before deletions, so a folder is emptied before it is removed
        self._run_all([i for i in copies if i.is_dir], self._copy, parallel=False)
        self._run_all([i for i in active if i.action.is_move], self._move, parallel=False)
        self._run_all([i for i in deletes if not i.is_dir], self._delete, parallel=True)
        self._run_all(sorted((i for i in deletes if i.is_dir), key=lambda i: i.depth, reverse=True),
                      self._delete, parallel=False)
        self._run_all([i for i in active if i.action.is_attr], self._attrs, parallel=True)
        self._run_all([i for i in copies if not i.is_dir], self._copy, parallel=True)
        free = self._to_free(items)
        if free:
            self._verify_free(free)
            self._run_all([i for i in free if not i.is_dir], self._free, parallel=True)
            self._run_all(sorted((i for i in free if i.is_dir), key=lambda i: i.depth, reverse=True),
                          self._free, parallel=False)
        for item in self.cmp.items:
            if item.covered and item.status == "":
                owner = parent(item.path)
                while owner and not self.cmp.by_path[owner].action.is_delete:
                    owner = parent(owner)
                if owner and self.cmp.by_path[owner].status == "done":
                    item.status = "done"

    def _run(self, items: list[Item]) -> SyncResult:
        started = time.monotonic()
        self.result.state_saved = False
        self.result.errors = [e for e in self.result.errors if not e.startswith(_STATE_ERROR)]
        try:
            self._execute(items)
            for n in range(1, self.job.retries + 1):
                failed = self.failed_items()
                if not failed:
                    break
                self.reporter.log(f"retrying {len(failed)} failed item(s) in {self.job.retry_delay:g} s "
                                  f"({n}/{self.job.retries})")
                if self.reporter.cancel_event.wait(self.job.retry_delay):
                    raise Cancelled()
                for side in (self.left, self.right):
                    try:
                        side.backend.ensure_connected()
                    except (OSError, SyncError) as e:
                        self.reporter.log(f"WARNING reconnecting to the {side.name} failed: {e}")
                self._execute(self._reset(failed))
        except Cancelled:
            self.result.cancelled = True
            self.reporter.log("synchronization cancelled")
        finally:
            try:
                self._save_state()
                self.result.state_saved = True
            except Exception as e:  # noqa: BLE001 - must be reported, never lost
                self.result.errors.append(f"{_STATE_ERROR}: {e}")
                self.reporter.log(f"ERROR {_STATE_ERROR}: {e}")
        if not self.result.cancelled:
            for side in (self.left, self.right):
                try:
                    self._purge_trash(side)
                except OSError as e:
                    self.reporter.log(f"WARNING trash cleanup on {side.name} failed: {e}")
        self.result.seconds += time.monotonic() - started
        return self.result


def _other(side: str) -> str:
    return "right" if side == "left" else "left"


def synchronize(cmp: Comparison, left: Backend, right: Backend,
                reporter: Reporter | None = None, force: bool = False) -> SyncResult:
    problem = cmp.safety_problem()
    if problem and not force:
        raise SyncError(problem)
    return Synchronizer(cmp, left, right, reporter).run()
