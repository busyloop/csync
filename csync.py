#!/usr/bin/env python3
"""csync — sync clean working copies of local Git repositories to a remote host via rsync.

Usage (rsync-style: sources first, destination last):
    csync [options] <repo-path>[@ref]... <remote-target>

Example:
    csync --ref develop repo1 repo2@master subdir/repo3 remotehost.example.com:/home/box

For each git repo, csync fetches the requested ref from its configured
remote, checks the resolved commit out into a temporary detached worktree,
and rsyncs that clean tree to <remote-target>/<repo-relative-path>/. It never
uses `git push` and never copies .git, untracked, or ignored files (unless
asked). Plain (non-git) directories are synced verbatim.

The ref for each repo is, in order of precedence: the `@ref` suffix on the
repo argument, the --ref option, or the repo's currently checked-out ref.
Because `@` separates the ref, repo paths containing `@` are not supported.
"""

import argparse
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


class UsageError(Exception):
    """Bad command line or bad input; exit code 2, nothing was touched."""


class SyncError(Exception):
    """A sync step failed for one source."""


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):  # do not sys.exit() from inside the parser
        raise UsageError(message)


def parse_args(argv):
    p = _Parser(
        prog="csync",
        usage="csync [options] <repo-path>[@ref]... <remote-target>",
        description="Sync clean working copies of local Git repos to a remote host via rsync.",
        add_help=True,
    )
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show what would be synced without changing remote files")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print commands and more detailed progress")
    p.add_argument("--no-delete", action="store_true",
                   help="do not delete remote files missing from source")
    p.add_argument("--ref", default=None, metavar="REF",
                   help="branch/ref to sync; default: each repo's currently checked-out ref")
    p.add_argument("--remote-name", default="origin", metavar="NAME",
                   help="git remote to fetch from (default: origin)")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="extra rsync exclude pattern; can be repeated")
    p.add_argument("--include-git", action="store_true",
                   help="also sync the repository's .git directory (default: off)")
    p.add_argument("positionals", nargs="*", metavar="ARG", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if len(args.positionals) < 2:
        raise UsageError("need at least one <repo-path> and a trailing <remote-target>")
    args.repos = args.positionals[:-1]
    args.remote_target = args.positionals[-1]
    if args.ref is not None and (args.ref.startswith("-") or not args.ref.strip()):
        raise UsageError("invalid ref: %r" % args.ref)
    if not args.remote_name.strip() or args.remote_name.startswith("-"):
        raise UsageError("invalid remote name: %r" % args.remote_name)
    return args


def split_repo_arg(arg):
    """'repo@ref' -> ('repo', 'ref'); 'repo' -> ('repo', None).

    The ref starts at the first '@', so paths containing '@' are unsupported
    (documented tradeoff) while refs like 'master@{1}' still work.
    """
    if "@" not in arg:
        return arg, None
    path, _, ref = arg.partition("@")
    if not path.strip():
        raise UsageError("empty repo path in %r" % arg)
    if not ref.strip() or ref.startswith("-"):
        raise UsageError("invalid ref in %r" % arg)
    return path, ref


def split_remote_target(target):
    """'host:/abs/path' -> (host, '/abs/path'). host may include user@."""
    if ":" not in target:
        raise UsageError("remote target must look like host:/absolute/path, got %r" % target)
    host, _, path = target.partition(":")
    if not host or "/" in host:
        raise UsageError("invalid remote host in %r" % target)
    if not path.startswith("/"):
        raise UsageError("remote path must be absolute (host:/absolute/path), got %r" % target)
    path = posixpath.normpath(path)
    return host, path


def repo_rel_path(arg, cwd):
    """Normalize a repo argument to a safe path relative to cwd.

    Accepts 'repo', './repo', 'repo/', 'subdir/repo', and absolute paths that
    live under cwd. Rejects anything that escapes cwd, since the relative path
    is reused verbatim under the remote base path.
    """
    if not arg.strip():
        raise UsageError("empty repo path")
    absolute = os.path.abspath(os.path.join(cwd, arg))
    rel = os.path.relpath(absolute, cwd)
    if rel == ".":
        raise UsageError("repo path %r resolves to the current directory" % arg)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        raise UsageError("repo path %r escapes the current directory" % arg)
    return rel.replace(os.sep, "/")


def remote_dir_for(base_path, rel):
    """Absolute remote directory a source is synced into (no host, no trailing /)."""
    return posixpath.join(base_path, rel)


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def run(cmd, verbose=False, cwd=None, capture=False):
    if verbose:
        print("+ " + shlex.join(cmd), flush=True)
    return subprocess.run(
        cmd, cwd=cwd, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def is_git_repo(path):
    res = run(["git", "-C", path, "rev-parse", "--git-dir"], capture=True)
    return res.returncode == 0


def current_ref(repo):
    """The currently checked-out branch name, or the HEAD commit if detached."""
    res = run(["git", "-C", repo, "symbolic-ref", "--quiet", "--short", "HEAD"], capture=True)
    if res.returncode == 0:
        return res.stdout.strip()
    res = run(["git", "-C", repo, "rev-parse", "HEAD"], capture=True)
    if res.returncode == 0:
        return res.stdout.strip()
    raise UsageError("cannot determine checked-out ref of %s (no commits yet?)" % repo)


def git_fetch(repo, remote_name, verbose):
    # No --tags: it hard-fails with "would clobber existing tag" whenever a
    # local tag diverged from the remote's. Default tag auto-following still
    # fetches new tags on fetched history and never clobbers.
    cmd = ["git", "-C", repo, "fetch", "--prune", remote_name]
    res = run(cmd, verbose=verbose, capture=not verbose)
    if res.returncode != 0:
        reason = ""
        stderr_lines = (res.stderr or "").strip().splitlines()
        if stderr_lines:
            reason = ": " + "; ".join(l.strip() for l in stderr_lines[-2:] if l.strip())
        raise SyncError("git fetch %s failed (exit %d)%s"
                        % (remote_name, res.returncode, reason))


def resolve_commit(repo, remote_name, ref, verbose):
    """Resolve ref to a commit, preferring the just-fetched remote-tracking ref."""
    candidates = ["refs/remotes/%s/%s" % (remote_name, ref), ref]
    for cand in candidates:
        res = run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", cand + "^{commit}"],
                  verbose=verbose, capture=True)
        if res.returncode == 0:
            return res.stdout.strip(), cand
    raise SyncError("cannot resolve ref %r (tried %s and %s)" % (ref, candidates[0], ref))


def add_worktree(repo, commit, path, verbose):
    res = run(["git", "-C", repo, "worktree", "add", "--detach", "--quiet", path, commit],
              verbose=verbose)
    if res.returncode != 0:
        raise SyncError("git worktree add failed (exit %d)" % res.returncode)


def remove_worktree(repo, path, verbose):
    run(["git", "-C", repo, "worktree", "remove", "--force", path],
        verbose=verbose, capture=not verbose)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    run(["git", "-C", repo, "worktree", "prune"], capture=True)


def git_dir_of(repo):
    res = run(["git", "-C", repo, "rev-parse", "--absolute-git-dir"], capture=True)
    if res.returncode != 0:
        raise SyncError("cannot locate .git directory of %s" % repo)
    return res.stdout.strip()


def rsync_supports_mkpath():
    try:
        res = subprocess.run(["rsync", "--help"], text=True, capture_output=True)
    except OSError:
        return False
    return "--mkpath" in (res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# rsync command construction (pure, unit-tested)
# ---------------------------------------------------------------------------

def build_rsync_cmd(src_dir, host, remote_dir, delete=True, dry_run=False,
                    verbose=False, excludes=(), exclude_git=True, mkpath=False):
    """Build the rsync argv syncing src_dir/ to host:remote_dir/.

    exclude_git anchors '/.git' at the transfer root: in a temporary worktree
    that entry is a pointer file into the source repo and must never be synced.
    When --mkpath is unavailable, remote parent directories are created via a
    wrapped --rsync-path (skipped on dry runs so they stay side-effect free).
    Dry runs itemize changes and compare by checksum (temp worktree mtimes are
    always fresh, so time-based comparison would flag every file); the output
    is captured and parsed rather than streamed.
    """
    cmd = ["rsync", "-a"]
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes", "--checksum"]
    elif verbose:
        cmd.append("-v")
    if delete:
        cmd.append("--delete")
    if exclude_git:
        cmd.append("--exclude=/.git")
    for pattern in excludes:
        cmd.append("--exclude=" + pattern)
    if mkpath:
        cmd.append("--mkpath")
    elif not dry_run:
        cmd += ["--rsync-path", "mkdir -p %s && rsync" % shlex.quote(remote_dir)]
    cmd.append(src_dir.rstrip("/") + "/")
    cmd.append("%s:%s/" % (host, remote_dir))
    return cmd


# itemize lines look like '>f+++++++++ path' / 'cd+++++++++ dir/' / '.f..t...... x'
ITEMIZE_RE = re.compile(r"^([<>ch.])([fdLDS])(\S{9,11}) (.+)$")


def parse_itemize(text):
    """Parse `rsync --dry-run --itemize-changes` output into (status, path) pairs.

    Statuses: would-create (all-'+' flags), would-update (content or link
    change), would-delete ('*deleting' lines). Attribute-only entries
    (leading '.') are skipped — with --checksum they are timestamp noise from
    the fresh worktree checkout. Non-itemize chatter is ignored.
    """
    changes = []
    for line in text.splitlines():
        if line.startswith("*deleting"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                changes.append(("would-delete", parts[1]))
            continue
        m = ITEMIZE_RE.match(line)
        if not m:
            continue
        update_type, ftype, flags, path = m.groups()
        if update_type == ".":
            continue
        if ftype == "L":
            path = path.split(" -> ")[0]
        if path in (".", "./"):
            continue
        if set(flags) == {"+"}:
            changes.append(("would-create", path))
        else:
            changes.append(("would-update", path))
    return changes


def format_diff_lines(changes, host, remote_dir, tty):
    """Render (status, path) pairs, one line per file: STATUS DESTINATION.

    tty: space-aligned columns; non-tty: tab-separated (cut-friendly).
    """
    lines = []
    for status, path in changes:
        dest = "%s:%s/%s" % (host, remote_dir, path)
        if tty:
            lines.append("%-12s  %s" % (status, dest))
        else:
            lines.append("%s\t%s" % (status, dest))
    return lines


def report_diff(output, host, remote_dir):
    changes = parse_itemize(output or "")
    for line in format_diff_lines(changes, host, remote_dir, sys.stdout.isatty()):
        print(line)
    sys.stdout.flush()
    return len(changes)


# ---------------------------------------------------------------------------
# syncing
# ---------------------------------------------------------------------------

def sync_repo(local, rel, ref, host, base_path, args, mkpath):
    """Sync one git repository at ref. Raises SyncError on any failure."""
    v = args.verbose
    remote_dir = remote_dir_for(base_path, rel)

    git_fetch(local, args.remote_name, v)
    commit, resolved = resolve_commit(local, args.remote_name, ref, v)
    if v:
        print("    resolved %s -> %s" % (resolved, commit[:12]), flush=True)

    tmp = tempfile.mkdtemp(prefix="csync-")
    worktree = os.path.join(tmp, "wt")
    try:
        add_worktree(local, commit, worktree, v)

        cmd = build_rsync_cmd(
            worktree, host, remote_dir,
            delete=not args.no_delete, dry_run=args.dry_run, verbose=v,
            excludes=args.exclude, exclude_git=True, mkpath=mkpath,
        )
        res = run(cmd, verbose=v, capture=args.dry_run)
        if res.returncode != 0:
            msg = "rsync failed (exit %d)" % res.returncode
            if args.dry_run and not mkpath:
                msg += " — note: on a dry run the remote directory is not pre-created; " \
                       "a real run creates it first"
            raise SyncError(msg)
        changes = 0
        if args.dry_run:
            changes += report_diff(res.stdout, host, remote_dir)

        if args.include_git:
            git_dir = git_dir_of(local)
            cmd = build_rsync_cmd(
                git_dir, host, remote_dir + "/.git",
                delete=not args.no_delete, dry_run=args.dry_run, verbose=v,
                excludes=(), exclude_git=False, mkpath=mkpath,
            )
            res = run(cmd, verbose=v, capture=args.dry_run)
            if res.returncode != 0:
                raise SyncError("rsync of .git failed (exit %d)" % res.returncode)
            if args.dry_run:
                changes += report_diff(res.stdout, host, remote_dir + "/.git")
    finally:
        remove_worktree(local, worktree, v)
        shutil.rmtree(tmp, ignore_errors=True)

    return "%s:%s/" % (host, remote_dir), commit, changes


def sync_plain(local, rel, host, base_path, args, mkpath):
    """Sync a non-git directory as-is. No fetch, no worktree."""
    remote_dir = remote_dir_for(base_path, rel)
    cmd = build_rsync_cmd(
        local, host, remote_dir,
        delete=not args.no_delete, dry_run=args.dry_run, verbose=args.verbose,
        excludes=args.exclude, exclude_git=not args.include_git, mkpath=mkpath,
    )
    res = run(cmd, verbose=args.verbose, capture=args.dry_run)
    if res.returncode != 0:
        raise SyncError("rsync failed (exit %d)" % res.returncode)
    changes = report_diff(res.stdout, host, remote_dir) if args.dry_run else 0
    return "%s:%s/" % (host, remote_dir), changes


SUMMARY_COLUMNS = ("status", "source", "ref", "commit", "destination", "detail")


def summary_rows(results):
    """One row of string fields per source, in SUMMARY_COLUMNS order.

    Empty fields become '-'; embedded whitespace is collapsed to single
    spaces so the column structure always holds. Statuses: synced,
    would-sync, failed, skipped.
    """
    rows = []
    for r in results:
        fields = [r["status"], r["source"], r["ref"] or "-", r["commit"] or "-",
                  r["dest"] or "-", r["detail"] or "-"]
        rows.append([" ".join(str(f).split()) for f in fields])
    return rows


def format_summary(rows, tty):
    """Render summary rows as lines of text.

    tty: space-aligned columns under an uppercase header, for humans.
    non-tty: a '--> summary' marker, a '#'-prefixed header, then one
    tab-separated line per source — the machine-readable contract
    (`cut -f1-6` friendly).
    """
    if tty:
        header = [c.upper() for c in SUMMARY_COLUMNS]
        widths = [max(len(row[i]) for row in [header] + rows)
                  for i in range(len(SUMMARY_COLUMNS))]
        return ["  ".join(f.ljust(w) for f, w in zip(row, widths)).rstrip()
                for row in [header] + rows]
    return (["--> summary", "# " + "\t".join(SUMMARY_COLUMNS)]
            + ["\t".join(row) for row in rows])


def print_summary(results, verbose):
    if verbose:
        print()
    for line in format_summary(summary_rows(results), sys.stdout.isatty()):
        print(line)
    sys.stdout.flush()


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        print("csync: error: %s" % exc, file=sys.stderr)
        print("usage: csync [options] <repo-path>[@ref]... <remote-target>", file=sys.stderr)
        return 2

    cwd = os.getcwd()
    try:
        for tool in ("git", "rsync"):
            if shutil.which(tool) is None:
                raise UsageError("required tool not found on PATH: %s" % tool)
        host, base_path = split_remote_target(args.remote_target)
        repos = []  # (rel, local, ref, is_git)
        seen = set()
        for arg in args.repos:
            path_part, explicit_ref = split_repo_arg(arg)
            rel = repo_rel_path(path_part, cwd)
            if rel in seen:
                raise UsageError("duplicate repo path: %r" % arg)
            seen.add(rel)
            local = os.path.join(cwd, rel)
            if not os.path.isdir(local):
                raise UsageError("no such directory: %r" % arg)
            if is_git_repo(local):
                ref = explicit_ref or args.ref or current_ref(local)
                repos.append((rel, local, ref, True))
            else:
                if explicit_ref:
                    raise UsageError(
                        "ref %r given for non-git directory %r" % (explicit_ref, path_part))
                repos.append((rel, local, None, False))
    except UsageError as exc:
        print("csync: error: %s" % exc, file=sys.stderr)
        return 2

    mkpath = rsync_supports_mkpath()
    ok_status = "would-sync" if args.dry_run else "synced"
    results = []
    failed = False
    for i, (rel, local, ref, is_git) in enumerate(repos, 1):
        result = {"source": rel, "ref": ref, "commit": None, "dest": None, "detail": None}
        if failed:
            result["status"] = "skipped"
            result["detail"] = "aborted after earlier failure"
            results.append(result)
            continue
        if args.verbose:
            if is_git:
                print("--> [%d/%d] %s @ %s" % (i, len(repos), rel, ref), flush=True)
            else:
                print("--> [%d/%d] %s (plain directory)" % (i, len(repos), rel), flush=True)
        try:
            if is_git:
                dest, commit, changes = sync_repo(local, rel, ref, host, base_path, args, mkpath)
                result["commit"] = commit[:12]
            else:
                dest, changes = sync_plain(local, rel, host, base_path, args, mkpath)
            if args.verbose:
                print("    %s -> %s" % (ok_status, dest), flush=True)
            result["status"] = ok_status
            result["dest"] = dest
            if args.dry_run:
                result["detail"] = "%d changes" % changes
        except SyncError as exc:
            print("csync: error: %s: %s" % (rel, exc), file=sys.stderr)
            result["status"] = "failed"
            result["detail"] = str(exc)
            failed = True
        results.append(result)

    print_summary(results, args.verbose)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
