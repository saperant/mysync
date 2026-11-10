"""Command line entry point.  Without a command the TUI starts."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

from mysync import __version__, fmt, hooks, paths
from mysync import config as config_mod
from mysync import selection as selection_mod
from mysync import state as state_mod
from mysync.backends import open_backend
from mysync.compare import Comparison, compare
from mysync.model import Action, SyncError
from mysync.runlog import RunLog
from mysync.sync import Reporter, synchronize

EXIT_OK, EXIT_ERRORS, EXIT_FATAL, EXIT_CONFLICTS = 0, 1, 2, 3


def _err(msg: str) -> None:
    print(f"mysync: {msg}", file=sys.stderr)


def editor_command(path) -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return [*shlex.split(editor), str(path)]


def cmd_list(_args) -> int:
    result = config_mod.load_all()
    if not result.jobs and not result.errors:
        print(f"No jobs in {paths.conf_dir()} - create one with: mysync new <name>")
    for job in result.jobs:
        last = state_mod.last_sync_time(job)
        when = fmt.mtime(last) if last else "never"
        print(f"{job.name:<20} {job.mode:<8} {job.left.url}  <->  {job.right.url}   (last sync: {when})")
    for e in result.errors:
        _err(e)
    return EXIT_ERRORS if result.errors else EXIT_OK


def _print_plan(cmp: Comparison, show_all: bool) -> None:
    for item in cmp.items:
        if item.via_move or item.moved_away:
            continue  # shown through the rename that handles them
        if (not show_all and (item.action == Action.NONE or item.covered)
                and not (item.conflict or item.name_fix)):
            continue
        sym = fmt.ACTION_SYMBOL[item.action]
        if item.action == Action.NONE and item.name_fix:
            sym = "Aa>" if item.name_fix == "right" else "<Aa"
        flag = "!" if item.conflict else " "
        path = item.path + ("/" if item.is_dir else "")
        reason = f"  ({item.reason})" if item.reason else ""
        print(f"{flag}{sym:>3}  {path}{reason}")
    c = cmp.counts()
    for warning in cmp.warnings:
        print(f"warning: {warning}")
    print(f"\n{cmp.state_message}")
    print(f"to right: {c[Action.COPY_LR.value]}, to left: {c[Action.COPY_RL.value]}, "
          f"renames: {c[Action.MOVE_R.value] + c[Action.MOVE_L.value]}, "
          f"attribute updates: {c[Action.ATTR_LR.value] + c[Action.ATTR_RL.value]}, "
          + (f"name spelling fixes: {c['names']}, " if c["names"] else "") +
          f"delete left: {c[Action.DELETE_L.value]}, delete right: {c[Action.DELETE_R.value]}, "
          f"conflicts: {c['conflict']}, transfer: {fmt.size(cmp.transfer_bytes())}")
    problem = cmp.safety_problem()
    if problem:
        print(f"refused unless forced: {problem}")
    if cmp.job.selective:
        print(f"free on the {cmp.job.selective}: {c[Action.EVICT_L.value] + c[Action.EVICT_R.value]} "
              f"item(s), {fmt.size(cmp.freed_bytes())}")


def _load_job(args) -> config_mod.JobConfig:
    job = config_mod.find_job(args.job)
    if getattr(args, "conflict", None):
        job.conflict = args.conflict
    return job


def cmd_compare(args) -> int:
    job = _load_job(args)
    with open_backend(job.left) as lb, open_backend(job.right) as rb:
        cmp = compare(job, lb, rb)
    if args.json:
        json.dump([{
            "path": i.path, "category": i.category.value, "action": i.action.value,
            "conflict": i.conflict, "reason": i.reason, "move_from": i.move_src if i.action.is_move else None,
            "left": i.left.to_list() if i.left else None,
            "right": i.right.to_list() if i.right else None,
        } for i in cmp.items if not (i.via_move or i.moved_away)
            and (args.all or i.action != Action.NONE or i.conflict)], sys.stdout, indent=1)
        print()
    else:
        _print_plan(cmp, args.all)
    return EXIT_CONFLICTS if cmp.counts()["conflict"] else EXIT_OK


class _CliReporter(Reporter):
    def __init__(self, log: RunLog, quiet: bool):
        super().__init__()
        self.runlog = log
        self.quiet = quiet

    def log(self, message: str) -> None:
        self.runlog.write(message)
        if not self.quiet or message.startswith(("ERROR", "WARNING")):
            print(message, file=sys.stderr if message.startswith("ERROR") else sys.stdout)


def cmd_sync(args) -> int:
    return _sync(_load_job(args), args)


def _sync(job: config_mod.JobConfig, args, only: list[str] | None = None,
          selection: selection_mod.Selection | None = None) -> int:
    if getattr(args, "retries", None) is not None:
        job.retries = args.retries
    try:
        return _sync_run(job, args, only, selection)
    except SyncError as e:
        if not args.dry_run:
            hooks.post_sync(job, "failed", error=str(e))
        raise


def _sync_run(job: config_mod.JobConfig, args, only: list[str] | None,
              selection: selection_mod.Selection | None) -> int:
    with open_backend(job.left) as lb, open_backend(job.right) as rb:
        cmp = compare(job, lb, rb, selection=selection)
        if only is not None:
            cmp.only(only)
        if args.dry_run:
            _print_plan(cmp, False)
            return EXIT_OK
        if not cmp.pending():
            if not args.quiet:
                print(f"{job.name}: nothing to do ({cmp.counts()['conflict']} conflict(s))")
            if cmp.counts()["conflict"]:
                hooks.post_sync(job, "conflicts", conflicts=cmp.counts()["conflict"])
                return EXIT_CONFLICTS
        log = RunLog(job.name)
        log.write(f"sync {job.name}: {job.left.url} <-> {job.right.url} ({job.mode})")
        log.write(cmp.state_message)
        reporter = _CliReporter(log, args.quiet)
        try:
            result = synchronize(cmp, lb, rb, reporter, force=args.force)
        except SyncError as e:
            log.write(f"ERROR {e}")
            log.close()
            raise
        for item in cmp.items:
            if item.conflict and item.action == Action.SKIP:
                reporter.log(f"WARNING conflict not resolved: {item.path} ({item.reason})")
        for warning in cmp.warnings:
            reporter.log(f"WARNING {warning}")
        summary = (f"{job.name}: {result.done} done, {result.failed} failed, "
                   + (f"{result.changed} changed meanwhile (compare again), " if result.changed else "")
                   + (f"{fmt.size(result.freed)} freed, " if result.freed else "")
                   + f"{cmp.counts()['conflict']} conflict(s), {fmt.size(result.bytes)} in "
                   f"{fmt.duration(result.seconds)}" + (" - CANCELLED" if result.cancelled else ""))
        log.write(summary)
        hooks.post_sync(job, hooks.status_of(result, cmp.counts()["conflict"]), result,
                        cmp.counts()["conflict"], str(log.path), log=reporter.log)
        log.close()
        if not args.quiet or result.failed:
            print(summary)
            print(f"log: {log.path}")
    if result.failed or result.changed or not result.state_saved:
        return EXIT_ERRORS
    return EXIT_CONFLICTS if cmp.counts()["conflict"] else EXIT_OK


def _selective_job(name: str) -> config_mod.JobConfig:
    job = config_mod.find_job(name)
    if not job.selective:
        raise config_mod.ConfigError(
            f"job {job.name!r} does not use selective sync; add  selective = \"left\"  (the side that "
            f"keeps only a selection) to {job.file}")
    return job


def _selection_path(job: config_mod.JobConfig, given: str) -> str:
    """A path relative to the selective location: given as such, or as a
    local path inside it (absolute or relative to the current directory)."""
    loc = job.left if job.selective == "left" else job.right
    if loc.scheme == "local":
        root = os.path.realpath(loc.path)
        here = os.path.realpath(os.path.expanduser(given))
        if os.path.isabs(os.path.expanduser(given)) or os.path.lexists(given):
            if here == root or here.startswith(root + os.sep):
                return selection_mod.norm(os.path.relpath(here, root)) if here != root else ""
            if os.path.isabs(os.path.expanduser(given)):
                raise config_mod.ConfigError(f"{given} is not inside {loc.path}")
    try:
        return selection_mod.norm(given)
    except ValueError as e:
        raise config_mod.ConfigError(str(e)) from e


def cmd_select(args) -> int:
    job = _selective_job(args.job)
    if not (args.keep or args.drop or args.list):
        from mysync.tui.app import MysyncApp
        MysyncApp(initial_job=job.name, select=True).run()
        return EXIT_OK
    sel = selection_mod.load(job)
    changes = [(_selection_path(job, g), True) for g in args.keep or []]
    changes += [(_selection_path(job, g), False) for g in args.drop or []]
    # outer paths first: a folder's rule must not wipe the rules inside it
    for path, value in sorted(changes, key=lambda c: (c[0].count("/") + bool(c[0]), c[0])):
        sel.set(path, value)
    if args.keep or args.drop:
        print(f"saved {selection_mod.save(job, sel)} - the next sync applies it")
    print(f"{job.selective} side keeps {'everything' if sel.default else 'nothing'} except:")
    for path, value in sorted(sel.rules.items()):
        if path:
            print(f"  {'+' if value else '-'} {path}")
    return EXIT_OK


def cmd_free(args) -> int:
    job = _selective_job(args.job)
    roots = [_selection_path(job, given) for given in args.paths]
    sel = selection_mod.load(job)
    for root in roots:
        sel.set(root, False)
    if not args.dry_run:
        selection_mod.save(job, sel)
    return _sync(job, args, only=roots, selection=sel)


def cmd_new(args) -> int:
    kwargs = {k: v for k, v in (("left", args.left), ("right", args.right)) if v}
    path = config_mod.write_template(args.name, **kwargs)
    print(f"created {path}")
    if args.edit:
        return subprocess.call(editor_command(path))
    return EXIT_OK


def cmd_edit(args) -> int:
    job = config_mod.find_job(args.job)
    return subprocess.call(editor_command(job.file))


def cmd_paths(_args) -> int:
    print(f"jobs:     {paths.conf_dir()}")
    print(f"defaults: {paths.global_config_file()}")
    print(f"state:    {paths.default_state_dir()} (default; per location 'state_dir')")
    print(f"logs:     {paths.log_dir()}")
    return EXIT_OK


def cmd_tui(args) -> int:
    from mysync.tui.app import MysyncApp
    MysyncApp(initial_job=getattr(args, "job", None)).run()
    return EXIT_OK


_EXIT_CODES = """\
exit codes:
  0  everything synchronized
  1  some items failed or changed meanwhile (compare again), or the state was not saved
  2  fatal error: bad configuration, a location is missing, a safety check refused
     the run (see --force), or the job is already running
  3  unresolved conflicts were left alone"""

_PLAN_SYMBOLS = """\
plan symbols:
  ->  <-     copy to the right / to the left
  mv> <mv    rename or move on the right / left (nothing is transferred)
  at> <at    only update time/permissions on the right / left (content identical)
  Aa> <Aa    give the right / left the other side's spelling of the name
  -x  x-     delete on the right / left (to the trash unless deletion = "delete")
  fr> <fr    free the copy on the right / left (selective sync, verified first)
  --         skipped;  "!" in front marks a conflict
  =          equal (shown with --all)"""


def _retries(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return n


def _sub(sub, name: str, help: str, description: str = "", epilog: str = ""):
    return sub.add_parser(name, help=help, description=description or help[:1].upper() + help[1:] + ".",
                          epilog=epilog or None, formatter_class=argparse.RawDescriptionHelpFormatter)


def _job_arg(s) -> None:
    s.add_argument("job", help="job name (see 'mysync list') or path to a .toml job file")


def _run_args(s) -> None:
    """Options shared by sync and free."""
    s.add_argument("-n", "--dry-run", action="store_true", help="only show the plan, change nothing")
    s.add_argument("-f", "--force", action="store_true",
                   help="run even when a safety check refuses: more than max_delete_percent "
                        "of a side would be deleted, or a side has too little free space")
    s.add_argument("-q", "--quiet", action="store_true",
                   help="print only warnings, errors and the summary of failed runs (for cron)")
    s.add_argument("--retries", type=_retries, metavar="N",
                   help="try failed items again up to N times, waiting retry_delay seconds "
                        "before each round (default: the job's 'retries', normally 2)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mysync", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Two-way folder synchronization between local folders, SFTP, SSH and rclone "
                    "remotes.\nWithout a command the terminal user interface starts.",
        epilog=f"""\
examples:
  mysync new photos --left ~/Pictures --right sftp://server/srv/photos -e
  mysync compare photos           show what would happen
  mysync sync photos -q           synchronize, e.g. from cron or a systemd timer
  mysync                          choose a job in the TUI

jobs are TOML files in ~/.config/mysync/conf.d/ ('mysync paths' shows all
locations); every option is described in the README.

{_EXIT_CODES}""")
    p.add_argument("--version", action="version", version=f"mysync {__version__}")
    sub = p.add_subparsers(dest="command", title="commands", metavar="COMMAND")

    s = _sub(sub, "tui", "start the terminal user interface (default)",
             "Start the terminal user interface.  Everything works with the mouse and the keyboard.")
    s.add_argument("job", nargs="?", help="open this job directly and compare it")
    s.set_defaults(func=cmd_tui)

    s = _sub(sub, "list", "list configured jobs and when they last ran")
    s.set_defaults(func=cmd_list)

    s = _sub(sub, "compare", "show what a sync would do (changes nothing)",
             "Scan both locations, relate them to the last synchronized state and print\n"
             "the plan.  Nothing is changed.",
             f"{_PLAN_SYMBOLS}\n\nexit code 3 when conflicts are left unresolved, otherwise 0 (2 on errors).")
    _job_arg(s)
    s.add_argument("-a", "--all", action="store_true", help="also list identical items")
    s.add_argument("--json", action="store_true", help="print the plan as JSON (one object per item)")
    s.add_argument("--conflict", choices=config_mod.CONFLICT_POLICIES,
                   help="resolve conflicts this way instead of the job's 'conflict' setting")
    s.set_defaults(func=cmd_compare)

    s = _sub(sub, "sync", "compare and synchronize (non-interactive)",
             "Compare and synchronize without asking.\n\n"
             "Refused (unless --force) when more than max_delete_percent of a side would be\n"
             "deleted or a side lacks the free space for what is copied to it.  Failed items\n"
             "are retried at the end of the run.  The job's post_sync_command runs afterwards.\n"
             "Every run is logged ('mysync paths' shows where).",
             f"""\
examples:
  mysync sync photos -n                    show the plan only
  mysync sync photos -q --conflict newer   newer side wins, quiet (cron)
  mysync sync photos --retries 0           do not retry failed items

{_PLAN_SYMBOLS}

{_EXIT_CODES}""")
    _job_arg(s)
    _run_args(s)
    s.add_argument("--conflict", choices=config_mod.CONFLICT_POLICIES,
                   help="resolve conflicts this way instead of the job's 'conflict' setting; "
                        "'ask' leaves them unresolved outside the TUI")
    s.set_defaults(func=cmd_sync)

    s = _sub(sub, "select", "choose what a selective job keeps locally (tree browser)",
             "Choose which folders and files the selective side of a job keeps\n"
             "(the job needs selective = \"left\" or \"right\").  Without options a tree\n"
             "browser opens; --keep and --drop change the selection directly.",
             """\
examples:
  mysync select projects                        open the tree browser
  mysync select projects --keep Archive/2019    download it again on the next sync
  mysync select projects --drop Archive/2020    stop keeping it (freed on the next sync)
  mysync select projects --list""")
    s.add_argument("job", help="a job with 'selective' set")
    s.add_argument("--keep", nargs="+", metavar="PATH", help="select these paths (no TUI)")
    s.add_argument("--drop", nargs="+", metavar="PATH", help="deselect these paths (no TUI)")
    s.add_argument("-l", "--list", action="store_true", help="show the current selection")
    s.set_defaults(func=cmd_select)

    s = _sub(sub, "free", "free local space: upload, verify, remove local copies, deselect",
             "Free local space in a selective job: upload local changes in the given paths,\n"
             "verify that the other side holds the same content (checksums where the data\n"
             "lives, otherwise size and time), then remove the local copies and deselect the\n"
             "paths.  Only the given paths are synchronized.",
             """\
examples:
  mysync free projects Archive/2019 Archive/2020    relative to the location root
  cd ~/Projects/Archive && mysync free projects 2021   or local paths
  mysync free -n projects Archive/2019              show what would happen""")
    s.add_argument("job", help="a job with 'selective' set")
    s.add_argument("paths", nargs="+", metavar="PATH",
                   help="folders/files (relative to the location root, or local paths inside it)")
    _run_args(s)
    s.set_defaults(func=cmd_free, conflict=None)

    s = _sub(sub, "new", "create a job file from a commented template",
             "Create ~/.config/mysync/conf.d/NAME.toml from a template that lists every option.",
             """\
locations:
  /path, ~/path, file:///path          local folder
  sftp://[user@]host[:port]/path       SFTP (keys or agent; /~/dir is relative to home)
  ssh://[user@]host[:port]/path        system ssh client and the server's shell tools
  rclone:remote:path                   any rclone remote (set up with 'rclone config')

example:
  mysync new photos --left ~/Pictures --right sftp://me@server.example.org/srv/photos -e""")
    s.add_argument("name", help="job name: letters, digits, '.', '_' and '-'")
    s.add_argument("--left", help="left location (default: ~/Documents)")
    s.add_argument("--right", help="right location (default: an sftp:// example to edit)")
    s.add_argument("-e", "--edit", action="store_true", help="open the new file in $EDITOR")
    s.set_defaults(func=cmd_new)

    s = _sub(sub, "edit", "open a job file in $EDITOR")
    _job_arg(s)
    s.set_defaults(func=cmd_edit)

    s = _sub(sub, "paths", "show configuration, state and log locations")
    s.set_defaults(func=cmd_paths)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = getattr(args, "func", cmd_tui)
    try:
        return func(args)
    except (config_mod.ConfigError, SyncError) as e:
        _err(str(e))
        return EXIT_FATAL
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
