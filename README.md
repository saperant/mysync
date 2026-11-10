# mysync

Two-way folder synchronization with a terminal UI (mouse supported) and TOML
configuration.  Locations can be local folders, SFTP, SSH, or anything rclone
can reach (Google Drive, OneDrive, Dropbox, S3, …).

```
pip install git+https://github.com/saperant/mysync
mysync                  # start the TUI
```

### In the myrcp workbench

The same jobs and engine also run as a plugin of [myrcp](https://github.com/saperant/myrcp),
a terminal workbench: the job list and one tab per job with its plan, next to your
editor, terminals and other tools.  The `ui` extra brings myrcp from PyPI, so there
is nothing to build:

```
pip install 'mysync[ui] @ git+https://github.com/saperant/mysync'
mysync-ui               # the workbench with mysync in it
```

`=` on a plan row opens the two copies in `$MYRCP_DIFF` (vimdiff by default), `e`
edits a job in a tab, and conflicts appear in the Problems view.

## Why another sync tool?

* **Deletions must propagate.** A stateless sync cannot tell "deleted on the
  left" from "new on the right", so it brings deleted files back.  mysync
  records what both sides looked like after each successful run and performs a
  three-way comparison: *deleted here, unchanged there* becomes a deletion, *changed on
  both sides* becomes a conflict.  rclone is used only as a dumb transport for cloud
  storage; every decision and all state belong to mysync.
* **Nothing is transferred twice.** Renamed or moved files and folders are
  renamed on the other side instead of being deleted and uploaded again.  A file whose
  content is identical (verified with checksums computed where the data lives)
  but whose modification time or permissions changed only gets its attributes updated.
* **No software on the other end.** Nothing has to be installed on the remote
  host, so there is no version coupling between the two machines.  SFTP needs only
  an SFTP server; the SSH backend uses standard shell tools.
* **Safe by default.** Deleted and overwritten files go to a trash folder on the
  same location, mass deletions are refused, a location that vanished (e.g. an unmounted
  disk) aborts the run, and missing or mismatched state falls back to a
  non-destructive first-run merge.  Every item is re-checked right before it is
  modified, and a job never runs twice at the same time.

## Files

| What | Where |
|---|---|
| Jobs (one per file) | `~/.config/mysync/conf.d/*.toml` |
| Defaults for all jobs (optional) | `~/.config/mysync/mysync.toml` |
| Sync state (default) | `~/.config/mysync/db/` |
| Logs | `~/.local/state/mysync/logs/` |

`MYSYNC_CONFIG_DIR` and `MYSYNC_LOG_DIR` override these; `XDG_CONFIG_HOME` and
`XDG_STATE_HOME` are honoured.  `mysync paths` prints them.

## Job file

```toml
# ~/.config/mysync/conf.d/photos.toml   (the job name defaults to the file name)
mode = "two-way"          # two-way | mirror (left -> right) | update (left -> right, never deletes)
compare = "time-size"     # time-size | size | content (SHA-256)
time_tolerance = 2        # seconds; covers FAT and SFTP whole-second timestamps
conflict = "ask"          # ask (decide in the TUI; skipped by the CLI) | skip | newer | left | right
deletion = "trash"        # trash | delete
trash_keep_days = 30      # purge trash sessions older than this (0 = keep)
symlinks = "skip"         # skip | follow | links (see "Symbolic links")
transfers = 4             # files processed in parallel
delta_transfer = true     # send only changed blocks of large files (ssh/sftp, see below)
delta_min_size = "4M"     # ... for files at least this big
max_delete_percent = 50   # refuse when a larger share of a side would be deleted (0 = no limit)
detect_moves = true       # replay renames/moves instead of delete + transfer
verify_before_copy = true # same size but different time: compare checksums first (see below)
sync_permissions = false  # also compare and copy permission bits (local, sftp, ssh)
case_sensitive = "auto"   # auto | true | false: are "Foo.txt" and "foo.txt" different files? (see below)
unicode_normalization = true  # "é" composed or decomposed (macOS) is the same name
ignore_time_shift = []    # e.g. [1]: times exactly 1 hour apart are equal (FAT disks and DST)
retries = 2               # failed items are tried again this many times ...
retry_delay = 5           # ... after waiting this many seconds
post_sync_command = ""    # shell command run after every sync (see below)
exclude = [".git/", "*.tmp", "node_modules/", "/cache/"]
include = []              # when non-empty only matching files are synced

[left]
url = "~/Pictures"

[right]
url = "sftp://me@server.example.org/srv/photos"
state_dir = ".mysync"                 # keep this side's state on the server itself
identity_file = "~/.ssh/id_ed25519"
host_key_policy = "strict"            # or "accept-new"
```

A location may also be given as a plain string: `left = "~/Pictures"`.

### Location URLs

| URL | Backend |
|---|---|
| `/path`, `~/path`, `file:///path` | local file system |
| `sftp://[user@]host[:port]/path` | SFTP via paramiko; `~/.ssh/config`, the agent and default keys are used. `/~/dir` is relative to the home directory |
| `ssh://[user@]host[:port]/path` | system `ssh` client (multiplexed). The remote needs a POSIX shell and its standard tools: GNU find/coreutils (Linux) or the BSD base system (`find`, `stat -f`; tested on FreeBSD 15); checksums need `sha256sum` |
| `rclone:remote:path` | any rclone remote (set it up with `rclone config` first) |

Location options: `state_dir`, `trash_dir` (relative to the location, default
`.mysync/trash`; a path like `../.mysync-trash` keeps it outside the synced
tree, e.g. out of a public web root; local/sftp/ssh only), `identity_file`, `known_hosts`, `host_key_policy`,
`connect_timeout`, `ssh_command` and `ssh_options` (ssh), `rclone` (binary) and
`rclone_flags` (rclone runs as one `rclone rcd` process per location, reached
through a private unix socket), `remote_hash` (sftp: set to `false` to never run shell
commands on the server).  Authentication must be non-interactive (keys or agent).

### Comparison, checksums, renames and attributes

`compare` decides when two files count as equal: `time-size` (default),
`size`, or `content` (checksums of everything that has the same size).

Checksums are computed where the data lives whenever possible, so verifying
does not mean downloading:

| Location | Checksums computed by |
|---|---|
| local | mysync itself (reads the local disk) |
| ssh / sftp | `sha256sum`/`sha1sum`/`md5sum` on the server (sftp needs shell access; SFTP-only accounts are detected) |
| rclone | the remote's native hashes, e.g. md5 on Google Drive or S3, sha1 on OneDrive; Dropbox has none in common |

mysync uses the best algorithm both sides support natively (sha256 > sha1 > md5).
In `content` mode, a side without a common native hash is read in full.

**Renames and moves** (`detect_moves`): a planned "delete X on the right" plus "copy Y
to the right" becomes "rename X to Y on the right" when the right's X has the same
content as the left's Y.

* When both sides can compute checksums remotely (or in `content` mode), the
  match is by size and checksum alone. The rename may then come with other
  changes: a file that was moved *and* touched or chmodded is renamed and gets its
  time/permissions updated afterwards. A file renamed on one side and only touched on the
  other is not a conflict either; the newer time wins.
* Without checksums only exact size + time matches are trusted.
* If several files match, the one with the same file id (inode or cloud id,
  from the previous state) or the same name is preferred.
* When a folder's entire content moved to the same relative places, it becomes
  one folder rename.
* This also works without previous state, e.g. the first mirror after
  reorganizing.  A renamed file that was also edited is transferred normally.
* Empty files are never matched by checksum: they are simply copied or deleted.

**Attribute-only changes** (`verify_before_copy`): when a file would be
copied but both sides have the same size, and both can compute checksums remotely, the checksums
are compared first; identical files just get the newer modification time (and
permissions with `sync_permissions`).  Touching files on both sides is then not a conflict.

With `sync_permissions = true`, permission bits are part of the comparison. A chmod
on one side is propagated as an attribute update, and copied files keep their mode.

In `time-size` mode a same-size edit within `time_tolerance` of the last sync is
still detected: when the saved state shows that exactly one side changed, the tolerance
does not hide it.

**Delta transfers** (`delta_transfer`, `delta_min_size`): when a large file
changed on one side and an older version exists on the other, both sides hash
the file in fixed blocks (on the server with `split --filter=sha256sum`). Only
changed blocks are sent and written into a copy of the old version. The
patched copy is verified against the source's block checksums before it replaces the
file; the old version goes to the trash as usual.  This works between local, ssh
and sftp (with shell access) locations and needs only GNU coreutils on the server.
It suits files edited in place (logs, databases, disk images, media tags); content
inserted near the start shifts every block, and mysync then copies the whole file.
Cloud remotes always transfer whole files.

### Names differing in case or Unicode form

Some storage ignores case: OneDrive, Dropbox, exFAT/NTFS disks and macOS
treat `Foo.txt` and `foo.txt` as one file. macOS also writes `é` decomposed
(`e` + combining accent), while Linux usually has it composed. mysync pairs
such names instead of treating them as two files:

* `case_sensitive = "auto"` (default) ignores case when either side does.
  rclone reports this for its remote. Other locations are probed by looking up a
  scanned name with its case swapped.  `true`/`false` set it explicitly.
* `unicode_normalization = true` (default) pairs composed and decomposed names.
* Each side keeps its own spelling. New files go into the folder as the
  other side spells it, and state files record each side's names.
* A rename that changes only case or Unicode form (`notes.txt` → `Notes.txt`)
  is repeated on the other side (`Aa▶`/`◀Aa` in the TUI, `Aa>`/`<Aa` in the CLI).
  Mirror and update give the right the left's spelling. Without previous state the
  spelling is only adjusted along with a copy, and is otherwise left as it is.
* Names that cannot be paired, such as `a.txt` and `A.txt` next to each other while
  case is ignored, are left alone on both sides (with everything inside them),
  with a warning.

### Time shifts

FAT and exFAT disks store local time, so after a daylight saving change every
timestamp can appear shifted by exactly one hour.  `ignore_time_shift = [1]` treats
files whose times are exactly that many hours apart (within `time_tolerance`) as
equal. A file whose time moved by exactly that amount since the last sync also counts as unchanged.
Several values are possible, e.g. `[1, 2]`; the default is off.

### Symbolic links

`symlinks = "skip"` (default) ignores links, and `"follow"` synchronizes what they point to.
`"links"` recreates links as links, but only relative links whose
target stays inside the synchronized folder (e.g. `latest -> releases/2.1`).
Absolute links, and links pointing outside, are skipped and reported as warnings.  Links are
not supported on rclone remotes.

### Safety while running

* **Re-check before modifying.** Right before a file is overwritten, deleted,
  renamed or gets new attributes, both sides are checked again (size and time). If
  something changed after the comparison, that item is left alone, reported as
  "changed meanwhile", and handled by the next comparison.
* **Free space.** The comparison asks each location for its free space
  (local disks, `df` on ssh/sftp servers, rclone remotes that report it). A sync that would
  copy more data to a side than it can hold is refused. Deletions and overwritten files only
  count as freed space with `deletion = "delete"`, because the trash lives on the same location.
  `--force` (or "Synchronize anyway" in the TUI) overrides this, like the deletion limit.
* **Retries.** Items that fail (e.g. after a dropped connection) are tried
  again at the end of the run, in up to `retries` rounds, waiting `retry_delay`
  seconds before each. Lost sftp connections are reopened first. Items that changed
  meanwhile are never retried; they need a new comparison.  In the TUI, **Retry
  failed** in the progress dialog runs the remaining failures again.
* **One run per job.** A lock in `$XDG_RUNTIME_DIR/mysync/` prevents two
  runs of the same folder pair (e.g. cron and the TUI) from overlapping. The lock
  file is removed when the run ends, whether it succeeded or failed, and the kernel releases the lock if
  the process dies.  The lock is per machine: two different machines syncing against
  the same server are not coordinated.

### Where the state lives

Each side keeps its own state file, named after a hash of the two locations.
`state_dir` decides where, per location:

* an absolute or `~` path → a directory on **this machine** (default `~/.config/mysync/db`);
* a relative path → a directory **inside the location** (e.g. `.mysync`), so the
  state travels with a removable disk or is shared by several machines syncing
  against the same server.

Both files carry the same session id.  If one is missing or they disagree, the
run is treated as a first sync: nothing is deleted, files present on only one side are
copied, and differing files become conflicts.

### Filters

Glob patterns, gitignore style: `*.tmp` matches at any depth, a leading `/` or
any inner `/` anchors to the root, a trailing `/` matches folders only, `**` spans folders.
`.mysync/`, the trash and state folders, and temporary files are always excluded.
Filters apply to both sides alike: excluded items are neither copied nor deleted anywhere.

### Selective sync and freeing space

For a client/server setup (a server holds all project archives, a laptop keeps
only some of them), mark the side that keeps a selection:

```toml
selective = "left"        # the left side keeps only the selection; the right holds everything
```

`mysync select <job>` (or **Selection…** / `t` in the job screen) opens a tree
of both sides. Click a box (or press Space) to select or deselect a folder or file;
click a folder name, its arrow, or press Enter to open it. The summary shows how
much the next sync will download and free.  Saving writes
`~/.config/mysync/selection/<job>.toml` and shows the resulting plan.

Unlike filters, a selection is one-sided:

* Unselected items are not downloaded.  Nothing outside the selection is ever
  deleted on the full side, even if it disappears locally.
* Local copies outside the selection are **freed**: removed locally only after
  the full side is verified to hold the same content.  Checksums are computed
  where the data lives (`sha256sum` on an ssh/sftp server, native hashes with
  rclone); without them, size and modification time must match.  Local changes
  and new local files are uploaded first and freed in the same run.  A local
  copy that the server has already replaced with a newer version is treated
  like an overwritten file (trash).  Conflicts are left alone until resolved.
* A folder is removed only when it is empty afterwards; anything excluded by
  filters keeps it (shown as kept).
* Selecting a folder again downloads it on the next sync.

Freeing space from the command line deselects the given paths and syncs only them:

```
mysync free projects Archive/2019 Archive/2020   # relative to the location root...
cd ~/Projects/Archive && mysync free projects 2021   # ...or local paths
mysync free -n projects Archive/2019               # show what would happen
mysync select projects --keep Archive/2019         # bring it back (or --drop, --list)
```

### Post-sync command

`post_sync_command` is run with `sh -c` after every synchronization (CLI and
TUI), also when nothing was to do or the run failed, e.g. to send a notification:

```toml
post_sync_command = 'notify-send "mysync $MYSYNC_JOB" "$MYSYNC_STATUS: $MYSYNC_DONE done, $MYSYNC_FAILED failed"'
```

| Variable | Value |
|---|---|
| `MYSYNC_STATUS` | `ok`, `conflicts` (some unresolved), `errors` (items failed or changed meanwhile), `cancelled`, `failed` (the run could not start or was aborted) |
| `MYSYNC_JOB`, `MYSYNC_LEFT`, `MYSYNC_RIGHT` | job name and location URLs |
| `MYSYNC_DONE`, `MYSYNC_FAILED`, `MYSYNC_CHANGED`, `MYSYNC_CONFLICTS`, `MYSYNC_BYTES` | counts and bytes transferred |
| `MYSYNC_LOG` | the run's log file (empty if none was written) |
| `MYSYNC_ERROR` | the fatal error, or the first item error |

Its output goes to the log. If it fails, mysync logs a warning; the exit code of
the sync does not change.

### Defaults file

Top-level keys in `~/.config/mysync/mysync.toml` apply to every job, the
`[location]` table to every location:

```toml
deletion = "trash"
exclude = [".DS_Store", "Thumbs.db"]

[location]
rclone = "~/bin/rclone"
```

## Using it

**TUI** (`mysync`, or `mysync tui <job>`): select a job and press Enter, click it again or press **Open**;
it is compared right away.  The table shows both sides, the planned action and the reason.

* Click the **Action** cell, or press `a`, to cycle through possible actions.
* **Space** ignores the highlighted (or marked) rows for this round only:
  nothing is done to them, they disappear from the list, and their last
  synchronized state is kept, so the next comparison shows the same changes again.  A folder takes its content along; a folder that would be
  deleted but holds ignored items is kept.  The "– ignored" checkbox shows them
  again, and Space takes them back.  Comparing again (also after a sync) clears it;
  the job file is not changed.
* **→ / ←** (or `>` / `<`, or the ──▶ ◀── buttons) choose the direction: make the
  right like the left, or the reverse.  That is a copy, or a deletion where the
  item exists only on the other side; planned renames and time-only updates are kept.
* **Several rows at once:** click the narrow first column or Ctrl+click a row to mark it,
  Shift+click to mark a range; on the keyboard `m` (or Insert) marks and moves down,
  Shift+↑/↓ extends the marking, Ctrl+A marks all shown rows, Escape clears the marks.
  Directions, the buttons (✖◀ ▶✖ Skip Default), `x` (skip) and `d` (default) apply
  to all marked rows, or to the highlighted row when nothing is marked.
* Click a highlighted row (or press Enter) for details and choices.
* After a run with failures, **Retry failed** in the progress dialog tries them again.
* Renames are shown as `mv▶`/`◀mv`; their source rows and the contents of
  renamed folders are listed under "equal/details". Time/permission-only updates are `at▶`/`◀at`.
* `n` resolves all open conflicts in favour of the newer side; `e` edits the job in `$EDITOR`.
* Checkboxes filter what is shown.  F5 compares, F9 synchronizes (with a summary to confirm).

**CLI** (for cron/systemd timers):

```
mysync list
mysync new photos --left ~/Pictures --right sftp://server/srv/photos -e
mysync compare photos [--all] [--json]
mysync sync photos [--dry-run] [--force] [--quiet] [--conflict newer] [--retries N]
mysync select photos [--keep PATH...] [--drop PATH...] [--list]   # selective sync
mysync free photos PATH... [--dry-run]                             # free local space
```

Exit codes: 0 ok, 1 some items failed or changed meanwhile, 2 fatal error (also
"job is already running"), 3 unresolved conflicts.  `mysync --help` and `mysync <command> --help`
list every option with examples, the plan symbols and the exit codes.

## How a run works

1. Both locations are scanned in parallel and the state is loaded. Names are paired
   (case and Unicode form, see above), and the free space of both sides is read.
2. Every path is classified against the state (created / modified / deleted /
   unchanged on each side) and an action is chosen according to the mode.
3. Folder consistency: a folder is not deleted while something inside it
   stays; a folder deleted as a whole is removed with one operation.
4. Names that differ only in case or Unicode form are fixed, then folders are
   created, renames are replayed, deletions run,
   attributes are updated and finally files are copied.  File deletions,
   attribute updates and copies run in `transfers` parallel workers. Files are
   written to a temporary name and renamed into place; modification times are
   preserved.  Overwritten and deleted files are moved to
   `<location>/.mysync/trash/<timestamp>/`, one folder per run.  With selective
   sync, local copies outside the selection are verified and freed last.
5. Failed items are retried (`retries`).  The new state is written even if
   the run was cancelled or items failed.  Finished work is recorded, and anything unfinished is compared again next time.

## License

MIT, see [LICENSE](LICENSE).  Dependencies: Textual and Rich (MIT), Paramiko
(LGPL-2.1, used as an unmodified library); rclone (MIT) is an optional external
program.

## Development

```
uv venv && uv pip install -e '.[test]'
pytest                                     # local + rclone (if installed), the myrcp plugin
MYSYNC_TEST_SSH_HOST=localhost pytest      # also SFTP and SSH (needs key login)
```

Not yet covered by the remote tests: free space through `df` on ssh/sftp and
`rclone about`, rclone's case-insensitivity report, and reconnecting sftp before a
retry round. The `df` script itself is tested with the local shell.

## Notes and limitations

* **Detecting case-insensitive storage.** With `case_sensitive = "auto"`, rclone
  remotes report it themselves. Local, ssh and sftp locations are probed through
  an existing name, so an *empty* folder on case-insensitive storage counts as
  case-sensitive until it holds something. This applies, for example, to a freshly formatted
  exFAT disk or an empty share on a Windows/macOS server. Set `case_sensitive = false`
  for such a target.
* **Filters and case.** `include`/`exclude` patterns are matched against each side's
  own spelling and are always case-sensitive, even when names are paired ignoring case.
* **Choosing a name fix by hand.** The TUI can skip a name fix, which keeps both spellings
  and shows the rename again next time, but cannot reverse its direction.
  Without previous state a spelling difference is only fixed along with a copy.
* **Retries take time.** With the default `retries = 2`, `retry_delay = 5`, a run
  with permanent errors (e.g. permission denied) finishes about 10 seconds later.
  Use `retries = 0` or `--retries 0` to fail at once.
* **The free space check is an estimate.** It counts the data copied to a side.
  It does not count compression, deduplication, quotas, or the temporary copies that
  parallel and delta transfers need for a while. It is only done where the storage
  reports free space: sftp needs shell access (and `remote_hash` not set to `false`),
  and rclone needs a remote that supports `rclone about`. Elsewhere it is skipped.
* **Reconnecting.** Before each retry round a dropped sftp connection is
  reopened. ssh runs every command through its multiplexed connection, which
  reconnects by itself.
* **post_sync_command runs to the end.** The run (and the TUI dialog) waits for it.
  Start long tasks in the background (`some-script &`).

## Not yet implemented

* Delta transfers for insertions (rsync-style rolling checksums); in-place edits are covered.
* Locking across machines that sync against the same server.
* Native Google Drive / OneDrive / Dropbox backends (rclone covers them today).
* File names containing control characters on rclone remotes (rclone re-encodes them).
* Size/date filters, scheduled runs from the TUI.
* Selective sync over plain ssh still lists the whole remote tree (the unselected
  parts are filtered afterwards); placeholders for unselected files (as in OneDrive's
  "online-only" files) are not possible without a virtual file system.
