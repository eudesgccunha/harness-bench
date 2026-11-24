"""Unit tests for config validation, sandbox wrapping, env files and workdir handling. In-process."""

import os
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb

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

    def test_harness_name_with_space_is_rejected(self) -> None:
        cfg = base_config()
        cfg["harnesses"]["bad name"] = cfg["harnesses"]["claude"]
        self.assert_rejected(cfg, "invalid harness name")

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


class MakeBaselineTests(unittest.TestCase):
    def test_returns_sha_of_a_single_commit_with_current_files(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a")
        sha = hb.make_baseline(work)
        self.assertEqual((sha, run_git(work, "rev-list", "--count", "HEAD")), (run_git(work, "rev-parse", "HEAD"), "1"))


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


class SaveDiffTests(unittest.TestCase):
    def test_diff_contains_new_and_changed_files(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = hb.make_baseline(work)
        (work / "a.txt").write_text("changed")
        (work / "new.txt").write_text("n")
        hb.save_diff(work, baseline, out)
        text = out.read_text()
        self.assertIn("a.txt", text)
        self.assertIn("new.txt", text)

    def test_unknown_baseline_is_reported_in_the_diff_file(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        hb.make_baseline(work)
        hb.save_diff(work, "0" * 40, out)
        self.assertIn("git diff failed", out.read_text())

    def test_directory_that_is_not_a_repo_is_reported_in_the_diff_file(self) -> None:
        out = fixtures.tmpdir(self) / "d.diff"
        hb.save_diff(fixtures.tmpdir(self), "abc", out)
        self.assertIn("git diff failed", out.read_text())

    def test_long_diff_is_truncated(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = hb.make_baseline(work)
        (work / "a.txt").write_text("b" * 1000)
        hb.save_diff(work, baseline, out, limit=50)
        self.assertTrue(out.read_text().endswith("[truncated]"))


class CheckLitellmUrlTests(unittest.TestCase):
    def test_https_and_localhost_http_are_allowed(self) -> None:
        for url in ("https://litellm.example.com", "http://localhost:4000", "http://127.0.0.1:1"):
            hb.check_litellm_url(url)

    def test_plain_http_to_a_remote_host_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            hb.check_litellm_url("http://litellm.example.com")


if __name__ == "__main__":
    unittest.main()
