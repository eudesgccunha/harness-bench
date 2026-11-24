"""Unit tests for config validation, sandbox wrapping, env files and workdir handling. In-process."""

import dataclasses
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
from hbench.graders import Change
from hbench.graders.diff_constraints import DiffConstraintsGrader

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def run_git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return p.stdout.strip()


def base_config(**overrides: Any) -> dict[str, Any]:
    cfg = fixtures.fake_config({})
    cfg.update(overrides)
    return cfg


class ValidateConfigTests(unittest.TestCase):
    def assert_rejected(self, cfg: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(SystemExit) as cm:
            hb.validate_config(cfg)
        self.assertIn(fragment, str(cm.exception.code))

    def test_valid_config_passes(self) -> None:
        hb.validate_config(base_config())

    def test_task_id_with_separator_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), id="a/b")])
        self.assert_rejected(cfg, "invalid task id")

    def test_task_id_with_leading_dot_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), id=".hidden")])
        self.assert_rejected(cfg, "invalid task id")

    def test_option_like_repo_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), repo="--upload-pack=x")])
        self.assert_rejected(cfg, "must not start with '-'")

    def test_absolute_protected_path_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), protected_paths=["/etc/passwd"])])
        self.assert_rejected(cfg, "must be relative")

    def test_protected_path_with_parent_reference_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), protected_paths=["a/../b"])])
        self.assert_rejected(cfg, "must be relative")

    def test_hidden_tests_without_a_hidden_tests_grader_warns(self) -> None:
        cfg = base_config()
        cfg["tasks"][0]["graders"] = [{"type": "command", "command": "true"}]
        with fixtures.quiet() as out:
            hb.validate_config(cfg)
        self.assertIn("task fizzbuzz sets 'hidden_tests'", out.getvalue())

    def test_hidden_tests_with_the_grader_or_the_default_graders_does_not_warn(self) -> None:
        cfg = base_config()
        with fixtures.quiet() as out:
            hb.validate_config(cfg)
            cfg["tasks"][0]["graders"] = [{"type": "hidden_tests"}, {"type": "command", "command": "true"}]
            hb.validate_config(cfg)
        self.assertEqual(out.getvalue(), "")

    def test_harness_name_with_space_is_rejected(self) -> None:
        cfg = base_config()
        cfg["harnesses"]["bad name"] = cfg["harnesses"]["claude"]
        self.assert_rejected(cfg, "invalid harness name")

    def test_unknown_cost_source_is_rejected_with_the_valid_names(self) -> None:
        self.assert_rejected(base_config(cost_source="bogus"), "auto, litellm, computed, reported")

    def test_report_knobs_in_range_are_accepted(self) -> None:
        for key, value in (("recommend_within_pp", 0), ("recommend_within_pp", 100), ("target_ci_width", 0.5)):
            with self.subTest(key=key, value=value):
                hb.validate_config(base_config(**{key: value}))
        hb.validate_config(base_config(target_ci_width=1))

    def test_report_knobs_out_of_range_or_not_numbers_are_rejected(self) -> None:
        bad = (
            ("recommend_within_pp", -1),
            ("recommend_within_pp", 101),
            ("recommend_within_pp", "5"),
            ("recommend_within_pp", True),
            ("target_ci_width", 0),
            ("target_ci_width", 1.5),
            ("target_ci_width", "0.2"),
            ("target_ci_width", True),
        )
        for key, value in bad:
            with self.subTest(key=key, value=value):
                self.assert_rejected(base_config(**{key: value}), key)

    def test_spend_key_in_env_passthrough_is_rejected(self) -> None:
        cfg = base_config(litellm={"base_url": "https://x", "api_key": "${SPEND}"}, env_passthrough=["SPEND"])
        self.assert_rejected(cfg, "spend key")

    def test_spend_key_in_harness_env_is_rejected(self) -> None:
        cfg = base_config(litellm={"base_url": "https://x", "api_key": "${SPEND}"})
        cfg["harnesses"]["pi"]["env"]["TOKEN"] = "Bearer ${SPEND}"
        self.assert_rejected(cfg, "spend key")


class SafeNameAndRedactTests(unittest.TestCase):
    def test_safe_replaces_separators_and_strips_leading_dots(self) -> None:
        self.assertEqual(hb.safe("../a/b c"), "_a_b_c")

    def test_safe_never_returns_empty(self) -> None:
        self.assertEqual(hb.safe("..."), "_")

    def test_redact_hides_long_secrets_only(self) -> None:
        text = hb.redact("value=hunter2-hunter2 short=abc", ["hunter2-hunter2", "abc", ""])
        self.assertEqual(text, "value=***REDACTED*** short=abc")


class BaseEnvTests(unittest.TestCase):
    def test_only_allowlisted_and_passthrough_variables_reach_the_agent(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.update(HB_PASS="1", HB_SECRET="2", SPEND="3")
        env = hb.base_env({"env_passthrough": ["HB_PASS", "HB_MISSING"], "litellm": {"api_key": "${SPEND}"}})
        self.assertEqual(("HB_PASS" in env, "HB_SECRET" in env, "SPEND" in env), (True, False, False))

    def test_spend_key_is_removed_even_when_allowlisted(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["TZ"] = "UTC"
        env = hb.base_env({"litellm": {"api_key": "${TZ}"}})
        self.assertNotIn("TZ", env)


class RmtreeInsideTests(unittest.TestCase):
    def test_removes_a_directory_inside_root(self) -> None:
        root = fixtures.tmpdir(self)
        child = root / "child"
        child.mkdir()
        hb.rmtree_inside(child, root)
        self.assertFalse(child.exists())

    def test_refuses_the_root_itself(self) -> None:
        root = fixtures.tmpdir(self)
        with self.assertRaises(RuntimeError):
            hb.rmtree_inside(root, root)
        self.assertTrue(root.exists())

    def test_refuses_a_path_outside_root(self) -> None:
        root, other = fixtures.tmpdir(self), fixtures.tmpdir(self)
        with self.assertRaises(RuntimeError):
            hb.rmtree_inside(other, root)
        self.assertTrue(other.exists())


class WrapTests(unittest.TestCase):
    def test_without_prefix_returns_argv_unchanged(self) -> None:
        self.assertEqual(hb.wrap({}, ["a", "b"], "/w", "/e"), ["a", "b"])

    def test_prefix_is_formatted_and_prepended(self) -> None:
        cfg = {"sandbox": {"prefix": ["box", "--env-file", "{env_file}", "-v", "{workdir}:/w"]}}
        self.assertEqual(hb.wrap(cfg, ["a"], "/w", "/e"), ["box", "--env-file", "/e", "-v", "/w:/w", "a"])


class WriteEnvFileTests(unittest.TestCase):
    def test_file_is_private_and_skips_host_only_and_multiline_values(self) -> None:
        path = fixtures.tmpdir(self) / "env"
        hb.write_env_file({"A": "1", "PATH": "/bin", "B": "x\ny", "HOME": "/h"}, path)
        self.assertEqual((path.read_text(), path.stat().st_mode & 0o777), ("A=1\n", 0o600))


class RunProcTests(unittest.TestCase):
    def test_returns_exit_code_and_streams(self) -> None:
        code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        rc, timed_out, out, err = hb.run_proc([fixtures.PY, "-c", code], None, os.environ.copy(), 30)
        self.assertEqual((rc, timed_out, out.strip(), err.strip()), (3, False, "out", "err"))

    def test_timeout_is_reported_with_partial_output(self) -> None:
        # The child flushes a readiness line before sleeping; 5 s leaves room for slow interpreter start-up.
        code = "import sys, time; print('partial', flush=True); print('e', file=sys.stderr, flush=True); time.sleep(60)"
        rc, timed_out, out, err = hb.run_proc([fixtures.PY, "-c", code], None, os.environ.copy(), 5)
        self.assertEqual((rc, timed_out, out.strip(), err.strip()), (-1, True, "partial", "e"))


class BuildCmdTests(unittest.TestCase):
    def test_provider_is_split_from_model(self) -> None:
        cmd = hb.build_cmd(["{provider}", "{model_name}", "{model}", "{prompt}"], "p/m", "go")
        self.assertEqual(cmd, ["p", "m", "p/m", "go"])

    def test_model_without_provider_has_empty_provider(self) -> None:
        self.assertEqual(hb.build_cmd(["[{provider}]", "{model_name}"], "m", "go"), ["[]", "m"])


class DoneKeysTests(unittest.TestCase):
    def test_missing_file_gives_no_keys(self) -> None:
        self.assertEqual(hb.done_keys(fixtures.tmpdir(self) / "none.jsonl"), set())

    def test_keys_identify_task_harness_model_rep(self) -> None:
        path = fixtures.tmpdir(self) / "r.jsonl"
        path.write_text('{"task": "t", "harness": "h", "model": "m", "rep": 2}\nnoise\n')
        self.assertEqual(hb.done_keys(path), {("t", "h", "m", 2)})


class ResolveWorkRootTests(unittest.TestCase):
    def test_work_dir_inside_the_repo_is_rejected(self) -> None:
        base = fixtures.tmpdir(self)
        with self.assertRaises(SystemExit) as cm:
            hb.resolve_work_root({"work_dir": str(base / "work")}, base)
        self.assertIn("inside the benchmark repo", str(cm.exception.code))

    def test_work_dir_outside_the_repo_is_created(self) -> None:
        base, outside = fixtures.tmpdir(self), fixtures.tmpdir(self)
        root = hb.resolve_work_root({"work_dir": str(outside / "w")}, base)
        self.assertEqual((root, root.is_dir()), (outside / "w", True))

    def test_default_is_a_fresh_temp_dir(self) -> None:
        root = hb.resolve_work_root({}, fixtures.tmpdir(self))
        self.addCleanup(root.rmdir)
        self.assertTrue(root.name.startswith("harness-bench-work-"))


class PrepareWorkdirTests(unittest.TestCase):
    def make_git_repo(self, base: Path) -> tuple[Path, str]:
        repo = base / "src"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text("old")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "one")
        old = run_git(repo, "rev-parse", "HEAD")
        (repo / "f.txt").write_text("new")
        run_git(repo, "commit", "-qam", "two")
        return repo, old

    def test_plain_directory_is_copied(self) -> None:
        base = fixtures.tmpdir(self)
        (base / "repo").mkdir()
        (base / "repo" / "a.txt").write_text("a")
        hb.prepare_workdir({"id": "t", "repo": "repo"}, base, base / "work")
        self.assertEqual((base / "work" / "a.txt").read_text(), "a")

    def test_git_source_is_checked_out_at_ref_without_history(self) -> None:
        base = fixtures.tmpdir(self)
        repo, old = self.make_git_repo(base)
        hb.prepare_workdir({"id": "t", "repo": str(repo), "ref": old}, base, base / "work")
        self.assertEqual(((base / "work" / "f.txt").read_text(), (base / "work" / ".git").exists()), ("old", False))

    def test_git_source_without_ref_uses_head_and_reuses_cache(self) -> None:
        base = fixtures.tmpdir(self)
        repo, _ = self.make_git_repo(base)
        task = {"id": "t", "repo": str(repo)}
        hb.prepare_workdir(task, base, base / "w1")
        hb.prepare_workdir(task, base, base / "w2")
        self.assertEqual((base / "w2" / "f.txt").read_text(), "new")

    def test_is_git_source_recognises_urls_and_ssh(self) -> None:
        self.assertEqual(
            [hb.is_git_source(s) for s in ("a.git", "https://x/y", "git@h:r", "plain")], [True] * 3 + [False]
        )


class GitBinTests(unittest.TestCase):
    def test_resolves_the_git_executable(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/git"):
            self.assertEqual(hb.git_bin(), "/usr/bin/git")

    def test_exits_when_git_is_missing(self) -> None:
        with mock.patch("shutil.which", return_value=None), self.assertRaises(SystemExit) as cm:
            hb.git_bin()
        self.assertEqual(cm.exception.code, "git not found on PATH")


def baseline_of(test: unittest.TestCase, work: Path) -> hb.Baseline:
    """Baseline of `work` whose git dir lives outside it, like the engine's."""
    baseline: hb.Baseline = hb.make_baseline(work, fixtures.tmpdir(test) / "baseline.git")
    return baseline


def sh(cwd: Path, script: str) -> None:
    """Run what the agent could run inside the workdir (a shell script, as the agent's user)."""
    subprocess.run(["sh", "-c", script], cwd=cwd, check=True, capture_output=True)


class MakeBaselineTests(unittest.TestCase):
    def test_returns_a_single_commit_with_current_files_and_leaves_no_git_in_the_workdir(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        count = subprocess.run(
            ["git", f"--git-dir={baseline.git_dir}", "rev-list", "--count", baseline.sha],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual((count, len(baseline.sha), (work / ".git").exists()), ("1", 40, False))

    def test_files_listed_in_gitignore_are_part_of_the_baseline(self) -> None:
        work = fixtures.tmpdir(self)
        (work / ".gitignore").write_text("secret.txt\n")
        (work / "secret.txt").write_text("s\n")
        baseline = baseline_of(self, work)
        (work / "secret.txt").write_text("changed\n")
        self.assertEqual([c.path for c in hb.collect_changes(baseline)], ["secret.txt"])


class RunStepTests(unittest.TestCase):
    def test_stdout_and_stderr_come_back_apart_and_redacted_and_the_log_has_both(self) -> None:
        work, log = fixtures.tmpdir(self), fixtures.tmpdir(self) / "step.log"
        script = "echo out-supersecret1; echo err-supersecret1 >&2; exit 3"
        step = hb.run_step({}, script, dict(os.environ), 30, log, work, work.parent / "env", ["supersecret1"])
        self.assertEqual(step, (3, False, "out-***REDACTED***\n", "err-***REDACTED***\n"))
        self.assertEqual(log.read_text(), "out-***REDACTED***\n\n--- stderr ---\nerr-***REDACTED***\n")


class HardenedGitTests(unittest.TestCase):
    def test_no_inherited_git_variable_reaches_git(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_CONFIG_COUNT": "1"}):
            env = hb.git_env()
        self.assertEqual(
            (sorted(k for k in env if k.startswith("GIT_")), env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]),
            (["GIT_ATTR_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"], "/dev/null", "1"),
        )

    def test_a_replace_ref_in_the_git_dir_does_not_change_the_diff(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a\n")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_text("b\n")
        (work / "b.txt").write_text("c\n")
        hb.staged_diff(baseline)  # stage the edits, then make the baseline commit "contain" them
        tree = hb.hardened_git(baseline.git_dir, work, "write-tree", check=True).stdout.strip()
        other = hb.hardened_git(baseline.git_dir, work, *GIT_ID, "commit-tree", "-m", "x", tree, check=True)
        subprocess.run(
            [
                "git",
                f"--git-dir={baseline.git_dir}",
                "update-ref",
                f"refs/replace/{baseline.sha}",
                other.stdout.strip(),
            ],
            check=True,
        )
        self.assertEqual(sorted(c.path for c in hb.collect_changes(baseline)), ["a.txt", "b.txt"])

    def test_the_users_global_config_and_excludes_cannot_hide_a_file(self) -> None:
        home = fixtures.tmpdir(self)
        (home / "ignore").write_text("conftest.py\n")
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home / 'ignore'}\n")
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a\n")
        baseline = baseline_of(self, work)
        (work / "conftest.py").write_text("evil\n")
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home)}):
            self.assertEqual([c.path for c in hb.collect_changes(baseline)], ["conftest.py"])


class SaveDiffTests(unittest.TestCase):
    def test_diff_contains_new_and_changed_files(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_text("changed")
        (work / "new.txt").write_text("n")
        hb.save_diff(baseline, out)
        text = out.read_text()
        self.assertIn("a.txt", text)
        self.assertIn("new.txt", text)

    def test_unknown_baseline_is_reported_in_the_diff_file(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        hb.save_diff(dataclasses.replace(baseline_of(self, work), sha="0" * 40), out)
        self.assertIn("git diff failed", out.read_text())

    def test_a_git_that_cannot_run_is_reported_in_the_diff_file(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        for error in (OSError("no git"), subprocess.TimeoutExpired("git", 1)):
            with mock.patch.object(hb, "staged_diff", side_effect=error):
                hb.save_diff(baseline, out)
            self.assertIn("git diff failed", out.read_text())

    def test_directory_that_is_not_a_repo_is_reported_in_the_diff_file(self) -> None:
        out = fixtures.tmpdir(self) / "d.diff"
        hb.save_diff(hb.Baseline(fixtures.tmpdir(self) / "none.git", fixtures.tmpdir(self), "abc"), out)
        self.assertIn("git diff failed", out.read_text())

    def test_long_diff_is_truncated(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_text("b" * 1000)
        hb.save_diff(baseline, out, limit=50)
        self.assertTrue(out.read_text().endswith("[truncated]"))

    def test_a_non_utf8_patch_is_saved_not_raised(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_bytes(b"caf\xe9\n")
        hb.save_diff(baseline, out)
        self.assertIn("a.txt", out.read_text())

    def test_a_nested_git_directory_is_noted_in_the_audit_diff(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        sh(work, "mkdir sub && cd sub && git init -q && echo x > f.txt")
        hb.save_diff(baseline, out)
        self.assertIn("nested .git: sub/.git", out.read_text())


class CollectChangesTests(unittest.TestCase):
    def repo(self) -> tuple[Path, hb.Baseline]:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("1\n2\n")
        (work / "gone.txt").write_text("x\n")
        return work, baseline_of(self, work)

    def test_added_and_deleted_lines_are_counted_per_file_including_new_files(self) -> None:
        work, baseline = self.repo()
        (work / "a.txt").write_text("1\nchanged\nmore\n")
        (work / "sub").mkdir()
        (work / "sub" / "new.txt").write_text("n\n")
        got = {c.path: (c.added, c.deleted) for c in hb.collect_changes(baseline)}
        self.assertEqual(got, {"a.txt": (2, 1), "sub/new.txt": (1, 0)})

    def test_a_rename_is_reported_as_a_delete_and_an_add(self) -> None:
        work, baseline = self.repo()
        (work / "gone.txt").rename(work / "moved.txt")
        got = {c.path: (c.added, c.deleted) for c in hb.collect_changes(baseline)}
        self.assertEqual(got, {"gone.txt": (0, 1), "moved.txt": (1, 0)})

    def test_a_binary_file_counts_as_more_than_any_line_limit(self) -> None:
        work, baseline = self.repo()
        (work / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.assertEqual(
            [(c.path, c.added, c.deleted) for c in hb.collect_changes(baseline)], [("blob.bin", hb.BINARY_LINES, 0)]
        )
        self.assertTrue(DiffConstraintsGrader({"max_changed_lines": 10**6}).violations(hb.collect_changes(baseline)))

    def test_a_path_with_a_newline_stays_one_path(self) -> None:
        work, baseline = self.repo()
        (work / "a\nb.txt").write_text("x\n")
        self.assertEqual([c.path for c in hb.collect_changes(baseline)], ["a\nb.txt"])

    def test_a_git_that_cannot_run_fails_closed(self) -> None:
        _, baseline = self.repo()
        for error in (OSError("no git"), subprocess.TimeoutExpired("git", 1)):
            with mock.patch.object(hb, "staged_diff", side_effect=error):
                self.assertEqual(hb.collect_changes(baseline), hb.UNKNOWN_CHANGES)

    def test_a_path_with_a_tab_stays_one_path(self) -> None:
        work, baseline = self.repo()
        (work / "a\tb.txt").write_text("x\n")
        self.assertEqual([(c.path, c.added) for c in hb.collect_changes(baseline)], [("a\tb.txt", 1)])
        self.assertEqual(
            hb.parse_numstat("1\t2\ta\tb\tc.txt\0-\t-\tbin\0"),
            (Change("a\tb\tc.txt", 1, 2), Change("bin", hb.BINARY_LINES, 0)),
        )

    def test_no_changes_gives_an_empty_tuple(self) -> None:
        _, baseline = self.repo()
        self.assertEqual(hb.collect_changes(baseline), ())

    def test_an_unreadable_diff_fails_closed_with_a_path_outside_the_workdir(self) -> None:
        work, baseline = self.repo()
        self.assertEqual(hb.collect_changes(dataclasses.replace(baseline, sha="0" * 40)), hb.UNKNOWN_CHANGES)
        missing = hb.Baseline(fixtures.tmpdir(self) / "none.git", work, "abc")
        self.assertEqual(hb.collect_changes(missing), hb.UNKNOWN_CHANGES)
        self.assertTrue(hb.UNKNOWN_CHANGES[0].path.startswith("../"))


class GitBypassTests(unittest.TestCase):
    """What an agent can do through the git plumbing in its workdir must not hide a change (C1 and C2)."""

    def setUp(self) -> None:
        self.work = fixtures.tmpdir(self)
        (self.work / "tests").mkdir()
        (self.work / "tests" / "test_a.py").write_text("x\n")
        (self.work / "src.py").write_text("a\n")
        self.baseline = baseline_of(self, self.work)

    def paths(self) -> list[str]:
        return sorted(c.path for c in hb.collect_changes(self.baseline))

    def forbidden_hit(self) -> bool:
        grader = DiffConstraintsGrader({"forbidden": ["tests/*", "conftest.py"]})
        return bool(grader.violations(hb.collect_changes(self.baseline)))

    def test_hiding_tricks_through_an_agent_repo_still_fail_diff_constraints(self) -> None:
        # Every script is something the agent can run in its workdir. None touches the baseline git dir.
        scripts = {
            "skip-worktree": "git init -q . && git add -A && git update-index --skip-worktree tests/test_a.py",
            "assume-unchanged": "git init -q . && git add -A && git update-index --assume-unchanged tests/test_a.py",
            "info/exclude": "git init -q . && echo tests/ >> .git/info/exclude",
            "core.excludesFile": "git init -q . && echo tests/ > ../ex && git config core.excludesFile ../ex",
            "git replace": (
                "git init -q . && git add -A && git -c user.name=a -c user.email=a@b commit -qm x"
                " && git replace $(git rev-list --max-parents=0 HEAD) HEAD"
            ),
        }
        for name, script in scripts.items():
            for keep_git in (True, False):
                with self.subTest(name, keep_git=keep_git):
                    sh(self.work, script + " && echo y >> tests/test_a.py")
                    if not keep_git:
                        shutil.rmtree(self.work / ".git")
                    self.assertTrue(self.forbidden_hit())
                    shutil.rmtree(self.work / ".git", ignore_errors=True)
                    (self.work / "tests" / "test_a.py").write_text("x\n")

    def test_a_deleted_agent_repo_leaves_the_edit_visible_by_path(self) -> None:
        sh(self.work, "git init -q . && git add -A && git update-index --skip-worktree tests/test_a.py")
        sh(self.work, "echo y >> tests/test_a.py && rm -rf .git")
        self.assertEqual(self.paths(), ["tests/test_a.py"])

    def test_a_root_gitignore_does_not_hide_a_new_or_edited_file(self) -> None:
        sh(self.work, "echo conftest.py > .gitignore && echo evil > conftest.py && echo y >> tests/test_a.py")
        self.assertEqual(self.paths(), [".gitignore", "conftest.py", "tests/test_a.py"])

    def test_a_nested_gitignore_does_not_hide_a_file_either(self) -> None:
        sh(self.work, "mkdir tests/sub && echo '*' > tests/sub/.gitignore && echo e > tests/sub/evil.py")
        self.assertEqual(self.paths(), ["tests/sub/.gitignore", "tests/sub/evil.py"])

    def test_a_nested_git_directory_fails_closed_and_cannot_hide_its_files(self) -> None:
        sh(self.work, "mkdir tests/vendored && cd tests/vendored && git init -q && echo evil > e.py")
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_a_nested_git_file_fails_closed(self) -> None:
        sh(self.work, "echo 'gitdir: /somewhere' > .git")
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_a_git_dir_symlink_fails_closed(self) -> None:
        sh(self.work, "mkdir -p a/b && ln -s /tmp a/b/.git")
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_the_workdir_git_config_is_never_read(self) -> None:
        marker = self.work.parent / f"{self.work.name}.marker"
        self.addCleanup(marker.unlink, True)
        script = (
            f"git init -q . && git config core.fsmonitor 'touch {marker}; false'"
            f" && git config filter.x.clean 'touch {marker}; cat' && git config diff.x.textconv 'touch {marker}; cat'"
            " && echo '* filter=x diff=x' > .gitattributes && echo y >> src.py"
        )
        sh(self.work, script)
        # The low-level calls run git even though the nested repo makes `collect_changes` fail closed.
        hb.staged_diff(self.baseline, "--numstat")
        hb.staged_diff(self.baseline, "--stat", "--patch")
        self.assertFalse(marker.exists())


class AttributesAndFailuresTests(unittest.TestCase):
    """What the agent can do to make `git add` fail or to skew the counts must never end in "no changes"."""

    def setUp(self) -> None:
        self.work = fixtures.tmpdir(self)
        (self.work / "tests").mkdir()
        (self.work / "tests" / "test_a.py").write_text("x\n")
        (self.work / "src.py").write_text("a\n")
        self.baseline = baseline_of(self, self.work)
        self.grader = DiffConstraintsGrader({"forbidden": ["tests/*"], "max_changed_lines": 5})

    def paths(self) -> list[str]:
        return sorted(c.path for c in hb.collect_changes(self.baseline))

    def test_a_working_tree_encoding_attribute_cannot_make_the_add_fail_open(self) -> None:
        sh(self.work, "printf '* working-tree-encoding=UTF-16\\n' > .gitattributes && echo y >> tests/test_a.py")
        self.assertEqual(self.paths(), [".gitattributes", "tests/test_a.py"])

    def test_a_diff_or_binary_attribute_does_not_zero_the_line_count(self) -> None:
        for attr in ("* -diff", "* binary"):
            with self.subTest(attr):
                sh(
                    self.work,
                    f"printf '{attr}\\n' > .gitattributes && python3 -c \"open('src.py','w').write('l\\\\n'*100)\"",
                )
                changes = hb.collect_changes(self.baseline)
                self.assertEqual(sum(c.added for c in changes if c.path == "src.py"), 100)
                self.assertTrue(self.grader.violations(changes))

    def test_a_failing_add_raises_and_collect_changes_fails_closed(self) -> None:
        sh(
            self.work, "echo y >> tests/test_a.py && git init -q sub && echo e > sub/e.py"
        )  # a nested repo without a commit: git add fails
        with self.assertRaises(hb.GitError) as cm:
            hb.staged_diff(self.baseline, "--stat")
        self.assertIn("git add failed", str(cm.exception))
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_a_failing_add_is_written_to_the_audit_diff_with_secrets_redacted(self) -> None:
        sh(self.work, "git init -q sub && echo e > sub/e.py")
        out = fixtures.tmpdir(self) / "d.diff"
        with mock.patch.object(hb, "staged_diff", side_effect=hb.GitError("git add failed: token-0123456789")):
            hb.save_diff(self.baseline, out, secrets=["token-0123456789"])
        self.assertIn("git add failed: ***REDACTED***", out.read_text())
        hb.save_diff(self.baseline, out)
        self.assertIn("git add failed", out.read_text())

    def test_an_unreadable_file_never_ends_as_no_changes(self) -> None:
        sh(self.work, "echo y >> tests/test_a.py && echo s > secret && chmod 000 secret")
        self.addCleanup(os.chmod, self.work / "secret", 0o600)
        changes = hb.collect_changes(self.baseline)
        self.assertTrue(changes == hb.UNKNOWN_CHANGES or "tests/test_a.py" in [c.path for c in changes])
        self.assertTrue(self.grader.violations(changes))

    def test_an_unreadable_tracked_file_never_ends_as_no_changes(self) -> None:
        sh(self.work, "echo y >> tests/test_a.py && chmod 000 src.py")
        self.addCleanup(os.chmod, self.work / "src.py", 0o600)
        self.assertTrue(self.grader.violations(hb.collect_changes(self.baseline)))

    def test_without_the_attributes_file_the_diff_fails_closed_and_says_so(self) -> None:
        sh(self.work, "echo y >> tests/test_a.py")
        out = fixtures.tmpdir(self) / "d.diff"
        (self.baseline.git_dir / "info" / "attributes").unlink()
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)
        hb.save_diff(self.baseline, out)
        self.assertIn("info/attributes", out.read_text())

    def test_a_baseline_that_could_not_write_the_attributes_file_fails_closed(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a\n")
        with mock.patch.object(hb, "write_info_attributes", side_effect=OSError("read-only")):
            baseline = baseline_of(self, work)
        (work / "a.txt").write_text("edited\n")
        self.assertEqual(hb.collect_changes(baseline), hb.UNKNOWN_CHANGES)

    def test_the_attributes_file_hides_every_attribute_that_changes_how_a_file_is_added(self) -> None:
        (self.work / ".gitattributes").write_text("* filter=x diff=y text eol=crlf working-tree-encoding=UTF-16\n")
        names = ["filter", "diff", "text", "eol", "working-tree-encoding", "ident", "merge"]
        out = subprocess.run(
            [
                "git",
                f"--git-dir={self.baseline.git_dir}",
                f"--work-tree={self.work}",
                "check-attr",
                *names,
                "--",
                "src.py",
            ],
            cwd=self.work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(out.count("unspecified"), len(names))


class AttributesWithoutAttrSourceTests(AttributesAndFailuresTests):
    """Every attribute test again on a git older than 2.40: `info/attributes` alone must hold."""

    def setUp(self) -> None:
        patcher = mock.patch.object(hb, "git_version", return_value=(2, 34))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_the_attr_source_option_is_not_passed(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            hb.hardened_git(self.work.parent, self.work, "status")
        self.assertFalse(any(a.startswith("--attr-source") for a in run.call_args.args[0]))

    def test_a_filter_attribute_does_not_run_or_change_a_file(self) -> None:
        sh(self.work, "printf '* filter=x ident\\n' > .gitattributes && echo y >> src.py")
        self.assertEqual(self.paths(), [".gitattributes", "src.py"])


class GitVersionTests(unittest.TestCase):
    def test_git_version_is_read_once_from_git_version_output(self) -> None:
        hb.git_version.cache_clear()
        self.addCleanup(hb.git_version.cache_clear)
        fake = subprocess.CompletedProcess([], 0, stdout="git version 2.39.5 (Apple Git-154)\n")
        with mock.patch.object(subprocess, "run", return_value=fake) as run:
            self.assertEqual((hb.git_version(), hb.git_version()), ((2, 39), (2, 39)))
        self.assertEqual(run.call_count, 1)
        hb.git_version.cache_clear()
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="weird")):
            self.assertEqual(hb.git_version(), (0, 0))


class FreshIndexAndEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = fixtures.tmpdir(self)
        (self.work / "a.txt").write_text("a\n")
        (self.work / "ignored.txt").write_text("i\n")
        (self.work / ".gitignore").write_text("ignored.txt\n")
        self.baseline = baseline_of(self, self.work)

    def test_skip_worktree_in_the_stored_index_does_not_hide_an_edit(self) -> None:
        git = [f"--git-dir={self.baseline.git_dir}", f"--work-tree={self.work}"]
        subprocess.run(["git", *git, "update-index", "--skip-worktree", "a.txt"], check=True, cwd=self.work)
        subprocess.run(["git", *git, "update-index", "--assume-unchanged", "ignored.txt"], check=True, cwd=self.work)
        (self.work / "a.txt").write_text("edited\n")
        (self.work / "ignored.txt").write_text("edited\n")
        self.assertEqual(sorted(c.path for c in hb.collect_changes(self.baseline)), ["a.txt", "ignored.txt"])

    def test_a_diff_leaves_the_stored_index_untouched(self) -> None:
        stored = self.baseline.git_dir / "index"
        before = stored.read_bytes()
        (self.work / "a.txt").write_text("edited\n")
        hb.collect_changes(self.baseline)
        hb.save_diff(self.baseline, fixtures.tmpdir(self) / "d.diff")
        self.assertEqual(stored.read_bytes(), before)

    def test_inherited_git_variables_do_not_change_the_result(self) -> None:
        (self.work / "a.txt").write_text("edited\n")
        env = {"GIT_DIR": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent/index", "GIT_WORK_TREE": "/nonexistent"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual([c.path for c in hb.collect_changes(self.baseline)], ["a.txt"])

    def test_a_file_a_gitignore_listed_before_the_baseline_is_not_a_change(self) -> None:
        self.assertEqual(hb.collect_changes(self.baseline), ())


class ProtectedPathTests(unittest.TestCase):
    TASK = {"protected_paths": ["tests_dir", "runner.py", "missing.txt"]}

    def setUp(self) -> None:
        self.work, self.snap = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (self.work / "tests_dir").mkdir()
        (self.work / "tests_dir" / "t.py").write_text("orig")
        (self.work / "runner.py").write_text("orig")

    def test_restore_undoes_edits_additions_and_deletions(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "tests_dir" / "t.py").write_text("hacked")
        (self.work / "tests_dir" / "extra.py").write_text("planted")
        (self.work / "runner.py").unlink()
        hb.restore_protected(self.TASK, self.work, self.snap)
        self.assertEqual(
            (
                (self.work / "tests_dir" / "t.py").read_text(),
                (self.work / "tests_dir" / "extra.py").exists(),
                (self.work / "runner.py").read_text(),
            ),
            ("orig", False, "orig"),
        )

    def test_bytecode_the_agent_leaves_in_a_protected_directory_is_removed_on_restore(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "tests_dir" / "__pycache__").mkdir()
        (self.work / "tests_dir" / "__pycache__" / "t.cpython-312.pyc").write_bytes(b"poison")
        hb.restore_protected(self.TASK, self.work, self.snap)
        self.assertEqual(sorted(p.name for p in (self.work / "tests_dir").iterdir()), ["t.py"])

    def test_path_replaced_by_a_symlink_is_restored_as_a_real_file(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "runner.py").unlink()
        (self.work / "runner.py").symlink_to("/etc/hostname")
        hb.restore_protected(self.TASK, self.work, self.snap)
        runner = self.work / "runner.py"
        self.assertEqual((runner.is_symlink(), runner.read_text()), (False, "orig"))

    def test_path_absent_at_snapshot_time_is_removed_on_restore(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "missing.txt").write_text("planted")
        hb.restore_protected(self.TASK, self.work, self.snap)
        self.assertFalse((self.work / "missing.txt").exists())

    def test_symlinked_protected_path_is_snapshotted_as_a_link(self) -> None:
        (self.work / "runner.py").unlink()
        (self.work / "runner.py").symlink_to("target")
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        self.assertTrue((self.snap / "runner.py").is_symlink())


class CheckLitellmUrlTests(unittest.TestCase):
    def test_https_and_localhost_http_are_allowed(self) -> None:
        for url in ("https://litellm.example.com", "http://localhost:4000", "http://127.0.0.1:1"):
            hb.check_litellm_url(url)

    def test_plain_http_to_a_remote_host_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            hb.check_litellm_url("http://litellm.example.com")


if __name__ == "__main__":
    unittest.main()
