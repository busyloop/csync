# csync - Context Sync

Sync clean working copies of one or more local directories — usually Git
repositories — to a remote host/path via `rsync`.

For a Git repository, `csync` does **not** push refs. It fetches the
requested branch/ref, checks the resolved commit out into a temporary
detached worktree (a pristine tree: no `.git`, no untracked files, no ignored
files, independent of your dirty working tree), and rsyncs that tree to the
remote target under the same relative path. Plain (non-git) directories are
rsynced verbatim.

## Requirements

- Python 3.8+
- `git` and `rsync` on `PATH` (locally; the remote end needs `rsync` and a
  POSIX shell as usual for rsync-over-ssh)

## Usage

rsync-style: sources first, destination last.

```
csync [options] <repo-path>[@ref]... <remote-target>
```

- `<repo-path>[@ref]...` — one or more local directory paths.
  Sources under the current directory keep their relative layout on the
  destination (`subdir/repo` → `<dest>/subdir/repo`); sources anywhere else
  (`/tmp/bar`, `~/code/foo`, `../elsewhere`) land under their basename
  (`~/code/foo` → `<dest>/foo`). Two sources mapping to the same destination
  name is an error. Append `@<ref>` to sync a git repo at a specific ref.
- `<remote-target>` — rsync destination base in `host:/absolute/path` form
  (`user@host:/path` works too). Always the **last** argument.
  `localhost:/path` is special: it writes directly to the local filesystem
  without ssh. (A bare path is deliberately not accepted as the destination —
  with sources and destination in one positional list, a forgotten
  destination must not silently turn your last source into the target. Use
  `user@localhost:/path` if you really want ssh to localhost.)

The ref synced for each git repo is, in order of precedence:

1. the `@<ref>` suffix on the repo argument,
2. the `--ref` option,
3. the repo's currently checked-out branch (or HEAD commit if detached).

Because `@` separates the ref, repo paths containing `@` in a directory name
are not supported (a deliberate tradeoff).

### Example

```bash
./csync.py --ref develop repo1 repo2@master subdir/repo3 me@remotehost:/home/box
```

This syncs `repo1` and `subdir/repo3` at `develop` and `repo2` at `master`.
Each repo's `origin` is fetched first and the ref resolved against it
(preferring `origin/<ref>`); the resulting trees land at:

```
remotehost:/home/box/repo1/
remotehost:/home/box/repo2/
remotehost:/home/box/subdir/repo3/
```

### Options

```
-n, --dry-run       Diff mode: list per-file changes without touching remote files
-v, --verbose       Print commands and more detailed progress
--no-delete         Do not delete remote files missing from source
--ref REF           Branch/ref to sync; default: each repo's checked-out ref
--remote-name NAME  Git remote to fetch from; default: origin
--exclude PATTERN   Extra rsync exclude pattern; can be repeated
--include-git       Also sync the repository's .git directory; default: off
```

More examples:

```bash
# Sync whatever each repo currently has checked out
./csync.py api worker deploy@web1:/srv/apps

# Preview a deploy of a tag, keeping remote-only files
./csync.py -n --no-delete --ref v2.4.0 api worker deploy@web1:/srv/apps

# Fetch from a non-default remote and skip logs
./csync.py --remote-name upstream --exclude '*.log' --ref main tools host:/srv/box

# Mix a git repo pinned to a release with a plain directory of static assets
./csync.py app@release assets host:/srv/box

# Local destination: no ssh, plain filesystem write
./csync.py api worker localhost:/srv/staging

# Sources can live anywhere; out-of-tree paths land under their basename
./csync.py ~/code/foo /tmp/bar localhost:/tmp/destination
```

## Output format

The result of a run is a single summary table with one row per source and
six columns: `status`, `source`, `ref`, `commit`, `destination`, `detail`.
Statuses: `synced`, `would-sync` (dry run), `failed`, `skipped`. The `ref`
and `commit` columns are `-` for plain directories; empty fields are `-`;
embedded whitespace in a field is collapsed to single spaces so the column
structure always holds.

**When stdout is a tty**, the table is space-aligned for humans:

```
STATUS  SOURCE        REF      COMMIT        DESTINATION                          DETAIL
synced  repo1         develop  00aed8065fff  remotehost:/home/box/repo1/          -
synced  assets        -        -             remotehost:/home/box/assets/         -
failed  subdir/repo3  main     -             -                                    cannot resolve ref 'main' (...)
```

**When stdout is not a tty** (piped/redirected), the table is strictly
tab-separated for easy parsing with `cut`, preceded by a `--> summary`
marker and a `#`-prefixed header:

```
--> summary
# status	source	ref	commit	destination	detail
synced	repo1	develop	00aed8065fff	remotehost:/home/box/repo1/	-
failed	subdir/repo3	main	-	-	cannot resolve ref 'main' (...)
```

Example parse:

```bash
csync ... | awk '/^--> summary/{f=1;next} f && !/^#/' | cut -f1,2,4
```

By default nothing else is printed to stdout; per-source progress lines
(`--> [1/3] repo1 @ develop`, resolved commits, rsync output) appear only
with `--verbose`. Errors are written to stderr, prefixed `csync: error:`.

### Dry-run diff

`--dry-run` is a diff mode: before the summary it prints one line per file
that differs between the source and the remote — `STATUS DESTINATION`, where
status is `would-create`, `would-update`, or `would-delete`. Like the
summary, lines are space-aligned on a tty and tab-separated otherwise.
Unchanged files are not listed; comparison is content-based
(`rsync --checksum`), so freshly checked-out worktree timestamps don't
produce false positives. The summary's `detail` column reports the change
count per source (`N changes`).

```
would-create	remotehost:/home/box/repo1/added.txt
would-update	remotehost:/home/box/repo1/hello.txt
would-delete	remotehost:/home/box/repo1/stale.txt
--> summary
# status	source	ref	commit	destination	detail
would-sync	repo1	master	87cfb9ac7b2d	remotehost:/home/box/repo1/	3 changes
```

Exit codes: `0` all sources synced, `1` a sync failed, `2` usage/validation
error.

## Behavior

- For each git repo: `git fetch --prune <remote>` (no `--tags`: fetching all
  tags hard-fails when a local tag diverged from the remote; default
  auto-following still picks up new tags on fetched history), resolve the
  ref to
  a commit (trying `refs/remotes/<remote>/<ref>` first, then `<ref>` for tags
  and commit hashes), create a temporary detached worktree at that commit,
  rsync it with `-a` (permissions preserved) and `--delete` (unless
  `--no-delete`), then remove the worktree.
- Repos without a usable remote still sync: when the repo has no remote of
  the configured name the fetch is skipped, and when the fetch fails
  (unreachable or deleted remote) a warning goes to stderr instead of
  failing the run. In both cases the ref is resolved locally only — a stale
  remote-tracking ref never shadows the local branch — and the summary's
  `detail` column records it (`local ref (no remote 'origin')` /
  `local ref (fetch failed)`).
- Plain (non-git) directories are rsynced verbatim — there is no notion of
  tracked/ignored files without git, so everything in them is synced except
  `--exclude` patterns. A `@ref` suffix on a plain directory is an error.
- Destination parent directories are created automatically (`--mkpath` when
  the local rsync supports it, otherwise a `mkdir -p` wrapped into
  `--rsync-path`; plain `os.makedirs` for `localhost:` destinations). On a
  dry run, destination directories are never pre-created, so syncing into a
  not-yet-existing directory can report an error that a real run would not
  hit.
- `--delete` does not remove an existing remote `.git` directory: `.git` is
  excluded from the transfer, and rsync leaves excluded paths on the receiver
  alone.
- `--include-git` rsyncs a repository's real `.git` directory alongside the
  tree. Note its `HEAD`/index reflect your local repository state, not the
  synced ref.
- Sources are processed in order. Validation problems (missing directory,
  unsafe path, bad remote target, bad ref syntax) abort before anything is
  synced. If a sync step fails mid-run, remaining sources are skipped and
  the exit code is non-zero.

## Install

### Homebrew (macOS and Linux)

```bash
brew install busyloop/tap/csync
```

Bleeding edge, straight from the default branch:

```bash
brew install --HEAD busyloop/tap/csync
```

### Manual

It's a single stdlib-only Python script — copy it anywhere on your `PATH`:

```bash
install -m 0755 csync.py /usr/local/bin/csync
```

## Tests

```bash
python3 -m unittest -v
```

Tests cover argument parsing, remote-target parsing, source-to-destination
path mapping (including collision and escape safety), rsync command
construction, and the summary output format. They run no git or rsync
commands.
