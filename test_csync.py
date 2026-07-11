#!/usr/bin/env python3
"""Unit tests for csync argument parsing, path mapping, and rsync command building."""

import unittest

import csync
from csync import UsageError


class TestParseArgs(unittest.TestCase):
    def test_basic(self):
        args = csync.parse_args(
            ["repo1", "repo2@master", "subdir/repo3", "remotehost.example.com:/home/box"])
        self.assertEqual(args.remote_target, "remotehost.example.com:/home/box")
        self.assertEqual(args.repos, ["repo1", "repo2@master", "subdir/repo3"])
        self.assertIsNone(args.ref)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.verbose)
        self.assertFalse(args.no_delete)
        self.assertFalse(args.include_git)
        self.assertEqual(args.remote_name, "origin")
        self.assertEqual(args.exclude, [])

    def test_ref_flag(self):
        args = csync.parse_args(["--ref", "develop", "repo", "h:/base"])
        self.assertEqual(args.ref, "develop")
        self.assertEqual(args.repos, ["repo"])
        self.assertEqual(args.remote_target, "h:/base")

    def test_options(self):
        args = csync.parse_args([
            "-n", "-v", "--no-delete", "--remote-name", "upstream",
            "--exclude", "*.log", "--exclude", "cache/", "--include-git",
            "--ref", "main", "repo", "h:/base",
        ])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.verbose)
        self.assertTrue(args.no_delete)
        self.assertTrue(args.include_git)
        self.assertEqual(args.ref, "main")
        self.assertEqual(args.remote_name, "upstream")
        self.assertEqual(args.exclude, ["*.log", "cache/"])

    def test_too_few_positionals(self):
        with self.assertRaises(UsageError):
            csync.parse_args(["h:/base"])
        with self.assertRaises(UsageError):
            csync.parse_args([])

    def test_bad_ref_flag(self):
        with self.assertRaises(UsageError):
            csync.parse_args(["--ref", "", "repo", "h:/base"])

    def test_unknown_option(self):
        with self.assertRaises(UsageError):
            csync.parse_args(["repo", "--delete-everything", "h:/base"])


class TestSplitRepoArg(unittest.TestCase):
    def test_no_ref(self):
        self.assertEqual(csync.split_repo_arg("repo1"), ("repo1", None))

    def test_with_ref(self):
        self.assertEqual(csync.split_repo_arg("repo2@master"), ("repo2", "master"))

    def test_subdir_with_ref(self):
        self.assertEqual(csync.split_repo_arg("subdir/repo3@v1.2.3"),
                         ("subdir/repo3", "v1.2.3"))

    def test_ref_may_contain_at(self):
        # split happens at the FIRST @, so reflog-style refs survive
        self.assertEqual(csync.split_repo_arg("repo@master@{1}"),
                         ("repo", "master@{1}"))

    def test_empty_ref_rejected(self):
        with self.assertRaises(UsageError):
            csync.split_repo_arg("repo@")

    def test_empty_path_rejected(self):
        with self.assertRaises(UsageError):
            csync.split_repo_arg("@master")

    def test_option_like_ref_rejected(self):
        with self.assertRaises(UsageError):
            csync.split_repo_arg("repo@-rf")


class TestSplitRemoteTarget(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(csync.split_remote_target("host:/home/box"),
                         ("host", "/home/box"))

    def test_user_at_host(self):
        self.assertEqual(csync.split_remote_target("deploy@h1.example.com:/srv/app"),
                         ("deploy@h1.example.com", "/srv/app"))

    def test_trailing_slash_normalized(self):
        self.assertEqual(csync.split_remote_target("h:/home/box/"),
                         ("h", "/home/box"))

    def test_root_path(self):
        self.assertEqual(csync.split_remote_target("h:/"), ("h", "/"))

    def test_missing_colon(self):
        with self.assertRaises(UsageError):
            csync.split_remote_target("/local/path")

    def test_relative_remote_path(self):
        with self.assertRaises(UsageError):
            csync.split_remote_target("h:relative/path")

    def test_empty_host(self):
        with self.assertRaises(UsageError):
            csync.split_remote_target(":/home/box")

    def test_local_path_with_colon_in_subdir(self):
        # 'foo/bar:baz' must not be mistaken for host 'foo/bar'
        with self.assertRaises(UsageError):
            csync.split_remote_target("foo/bar:/x")

    def test_rsync_daemon_syntax_rejected(self):
        with self.assertRaises(UsageError):
            csync.split_remote_target("h::module")


class TestMapSource(unittest.TestCase):
    CWD = "/work/dir"

    def map(self, arg):
        return csync.map_source(arg, self.CWD)

    # sources under cwd keep their relative layout
    def test_plain(self):
        self.assertEqual(self.map("repo1"), ("/work/dir/repo1", "repo1"))

    def test_dot_slash(self):
        self.assertEqual(self.map("./repo")[1], "repo")

    def test_trailing_slash(self):
        self.assertEqual(self.map("repo/")[1], "repo")

    def test_subdir_layout_preserved(self):
        self.assertEqual(self.map("subdir/repo3"),
                         ("/work/dir/subdir/repo3", "subdir/repo3"))

    def test_inner_dotdot_collapsed(self):
        self.assertEqual(self.map("a/../b")[1], "b")

    def test_absolute_inside_cwd_keeps_layout(self):
        self.assertEqual(self.map("/work/dir/sub/repo"),
                         ("/work/dir/sub/repo", "sub/repo"))

    # sources outside cwd map to their basename
    def test_absolute_outside_cwd_uses_basename(self):
        self.assertEqual(self.map("/tmp/bar"), ("/tmp/bar", "bar"))

    def test_absolute_trailing_slash(self):
        self.assertEqual(self.map("/tmp/bar/"), ("/tmp/bar", "bar"))

    def test_parent_path_uses_basename(self):
        self.assertEqual(self.map("../elsewhere"),
                         ("/work/elsewhere", "elsewhere"))

    def test_sneaky_escape_uses_basename(self):
        self.assertEqual(self.map("sub/../../elsewhere")[1], "elsewhere")

    def test_cwd_itself_uses_own_name(self):
        self.assertEqual(self.map("."), ("/work/dir", "dir"))

    def test_layout_never_contains_dotdot(self):
        # every input either maps to a '..'-free layout path or is rejected
        for arg in ("..", "../..", "/tmp/x", "sub/../../x", "."):
            try:
                _, rel = self.map(arg)
            except UsageError:
                continue
            self.assertNotIn("..", rel.split("/"))

    # degenerate inputs
    def test_root_rejected(self):
        with self.assertRaises(UsageError):
            self.map("/")

    def test_empty_rejected(self):
        with self.assertRaises(UsageError):
            self.map("")


class TestRefCandidates(unittest.TestCase):
    def test_fetched_prefers_remote_tracking_ref(self):
        self.assertEqual(csync.ref_candidates("origin", "master", fetched=True),
                         ["refs/remotes/origin/master", "master"])

    def test_not_fetched_resolves_locally_only(self):
        # stale remote-tracking refs must not shadow the local branch
        self.assertEqual(csync.ref_candidates("origin", "master", fetched=False),
                         ["master"])

    def test_custom_remote_name(self):
        self.assertEqual(csync.ref_candidates("upstream", "main", fetched=True)[0],
                         "refs/remotes/upstream/main")


class TestRemoteDir(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(csync.remote_dir_for("/home/box", "subdir/repo3"),
                         "/home/box/subdir/repo3")

    def test_root_base(self):
        self.assertEqual(csync.remote_dir_for("/", "repo"), "/repo")


class TestBuildRsyncCmd(unittest.TestCase):
    def test_defaults(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "h", "/base/repo", mkpath=True)
        self.assertEqual(cmd, [
            "rsync", "-a", "--delete", "--exclude=/.git", "--mkpath",
            "/tmp/wt/", "h:/base/repo/",
        ])

    def test_no_delete_dry_run(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "h", "/base/repo",
                                      delete=False, dry_run=True, verbose=True, mkpath=True)
        self.assertIn("--dry-run", cmd)
        self.assertIn("--itemize-changes", cmd)
        self.assertIn("--checksum", cmd)
        self.assertNotIn("-v", cmd)  # dry-run output is parsed, not streamed
        self.assertNotIn("--delete", cmd)

    def test_verbose_real_run_streams(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "h", "/base/repo",
                                      verbose=True, mkpath=True)
        self.assertIn("-v", cmd)
        self.assertNotIn("--itemize-changes", cmd)
        self.assertNotIn("--checksum", cmd)

    def test_extra_excludes_after_git_exclude(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "h", "/base/repo",
                                      excludes=["*.log", "cache/"], mkpath=True)
        self.assertLess(cmd.index("--exclude=/.git"), cmd.index("--exclude=*.log"))
        self.assertIn("--exclude=cache/", cmd)

    def test_include_git_drops_exclude(self):
        cmd = csync.build_rsync_cmd("/repo/.git", "h", "/base/repo/.git",
                                      exclude_git=False, mkpath=True)
        self.assertNotIn("--exclude=/.git", cmd)

    def test_mkdir_fallback_without_mkpath(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "h", "/base/my repo", mkpath=False)
        i = cmd.index("--rsync-path")
        self.assertEqual(cmd[i + 1], "mkdir -p '/base/my repo' && rsync")

    def test_dry_run_without_mkpath_has_no_mkdir(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "h", "/base/repo",
                                      dry_run=True, mkpath=False)
        self.assertNotIn("--rsync-path", cmd)

    def test_local_dest_has_bare_path_and_no_ssh_helpers(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "localhost", "/base/repo",
                                      mkpath=True, local_dest=True)
        self.assertEqual(cmd[-1], "/base/repo/")
        self.assertNotIn("--mkpath", cmd)
        self.assertNotIn("--rsync-path", cmd)

    def test_local_dest_without_mkpath_has_no_mkdir_wrapper(self):
        cmd = csync.build_rsync_cmd("/tmp/wt", "localhost", "/base/repo",
                                      mkpath=False, local_dest=True)
        self.assertNotIn("--rsync-path", cmd)

    def test_trailing_slash_on_source(self):
        cmd = csync.build_rsync_cmd("/tmp/wt/", "h", "/base/repo", mkpath=True)
        self.assertEqual(cmd[-2], "/tmp/wt/")


class TestParseItemize(unittest.TestCase):
    OUTPUT = "\n".join([
        "sending incremental file list",       # -v chatter: ignored
        "created directory /base/repo",        # --mkpath chatter: ignored
        ".d..t...... ./",                      # transfer-root dir: ignored
        ">f+++++++++ added.txt",
        ">f.st...... hello.txt",
        "cd+++++++++ bin/",
        ">f+++++++++ bin/run 2.sh",            # name with a space survives
        ".f..t...... unchanged.txt",           # attr-only (worktree mtime): ignored
        "cL+++++++++ link -> target",
        "*deleting   stale.txt",
        "*deleting   old dir/",
        "",
        "sent 202 bytes  received 44 bytes",   # stats: ignored
    ])

    def test_full_output(self):
        self.assertEqual(csync.parse_itemize(self.OUTPUT), [
            ("would-create", "added.txt"),
            ("would-update", "hello.txt"),
            ("would-create", "bin/"),
            ("would-create", "bin/run 2.sh"),
            ("would-create", "link"),
            ("would-delete", "stale.txt"),
            ("would-delete", "old dir/"),
        ])

    def test_empty(self):
        self.assertEqual(csync.parse_itemize(""), [])


class TestFormatDiffLines(unittest.TestCase):
    CHANGES = [("would-create", "added.txt"), ("would-delete", "stale.txt")]

    def test_non_tty_tab_separated(self):
        lines = csync.format_diff_lines(self.CHANGES, "h", "/base/repo", tty=False)
        self.assertEqual(lines, [
            "would-create\th:/base/repo/added.txt",
            "would-delete\th:/base/repo/stale.txt",
        ])

    def test_tty_aligned(self):
        lines = csync.format_diff_lines(self.CHANGES, "h", "/base/repo", tty=True)
        self.assertEqual(lines[0], "would-create  h:/base/repo/added.txt")
        self.assertEqual(lines[0].index("h:"), lines[1].index("h:"))
        self.assertNotIn("\t", "".join(lines))


def _result(**kw):
    base = {"status": "synced", "source": "repo1", "ref": "develop",
            "commit": "00aed8065fff", "dest": "h:/box/repo1/", "detail": None}
    base.update(kw)
    return base


class TestSummaryRows(unittest.TestCase):
    def test_fields_in_column_order(self):
        rows = csync.summary_rows([_result()])
        self.assertEqual(rows, [
            ["synced", "repo1", "develop", "00aed8065fff", "h:/box/repo1/", "-"],
        ])

    def test_empty_fields_become_dashes(self):
        rows = csync.summary_rows([
            _result(source="assets", ref=None, commit=None, dest="h:/box/assets/"),
            _result(status="skipped", source="repo2", ref="main", commit=None,
                    dest=None, detail="aborted after earlier failure"),
        ])
        self.assertEqual(rows[0], ["synced", "assets", "-", "-", "h:/box/assets/", "-"])
        self.assertEqual(rows[1],
                         ["skipped", "repo2", "main", "-", "-", "aborted after earlier failure"])

    def test_whitespace_sanitized(self):
        rows = csync.summary_rows([
            _result(status="failed", dest=None, detail="multi\nline\terror   message"),
        ])
        self.assertEqual(rows[0][5], "multi line error message")


class TestFormatSummary(unittest.TestCase):
    ROWS = [
        ["synced", "repo1", "develop", "00aed8065fff", "h:/box/repo1/", "-"],
        ["failed", "subdir/repo3", "main", "-", "-", "cannot resolve ref"],
    ]

    def test_non_tty_is_tab_separated_with_marker_and_header(self):
        lines = csync.format_summary(self.ROWS, tty=False)
        self.assertEqual(lines[0], "--> summary")
        self.assertEqual(lines[1], "# status\tsource\tref\tcommit\tdestination\tdetail")
        self.assertEqual(lines[2],
                         "synced\trepo1\tdevelop\t00aed8065fff\th:/box/repo1/\t-")
        for line in lines[2:]:
            self.assertEqual(len(line.split("\t")), 6)

    def test_tty_is_aligned(self):
        lines = csync.format_summary(self.ROWS, tty=True)
        self.assertEqual(lines[0].split(), ["STATUS", "SOURCE", "REF", "COMMIT",
                                            "DESTINATION", "DETAIL"])
        self.assertNotIn("\t", "".join(lines))
        # every column starts at the same offset on every line
        self.assertEqual(lines[0].index("SOURCE"), lines[1].index("repo1"))
        self.assertEqual(lines[0].index("SOURCE"), lines[2].index("subdir/repo3"))
        self.assertEqual(lines[0].index("REF"), lines[1].index("develop"))
        self.assertEqual(lines[0].index("DESTINATION"), lines[1].index("h:/box/repo1/"))

    def test_tty_has_no_marker(self):
        lines = csync.format_summary(self.ROWS, tty=True)
        self.assertNotIn("--> summary", lines)


if __name__ == "__main__":
    unittest.main()
