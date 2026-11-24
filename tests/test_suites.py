"""Tests for `hbench.suites`: the suite format, the loader and the expansion into engine tasks."""

import json
import os
import unittest
from pathlib import Path
from typing import Any

import fixtures
from hbench import cli, suites
from hbench.graders import grade_plan

SUITES = fixtures.ROOT / "suites"
EXAMPLE_SUITES = (SUITES / "regression", SUITES / "examples-capability")
# Built at runtime: this file is not a hidden test, so it must not hold the marker the contamination probe looks for.
MARKER = "HIDDEN-TEST" + "-MARKER"


def task_entry(**overrides: Any) -> dict[str, Any]:
    """A valid task entry of a suite file (paths relative to the suite directory)."""
    entry: dict[str, Any] = {
        "id": "fizz",
        "repo": "fizz/repo",
        "prompt": "fix it",
        "hidden_tests": "fizz/hidden_tests",
        "test": "python3 -m unittest -q",
        "solution": "fizz/solution",
        "tags": ["bugfix", "python"],
        "provenance": {"source": "synthetic", "license": "MIT", "url": None},
    }
    entry.update(overrides)
    return entry


def suite_file(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "1.0.0",
        "name": "demo",
        "version": "1.2.3",
        "kind": "capability",
        "description": "A demo suite.",
        "tasks": [task_entry()],
    }
    doc.update(overrides)
    return doc


def write_suite(root: Path, doc: Any = None, dirs: tuple[str, ...] = ()) -> Path:
    """A suite directory under `root` holding `doc` (default: a valid one) and the directories of task `fizz`."""
    suite_dir = root / "demo"
    for rel in dirs or ("fizz/repo", "fizz/hidden_tests", "fizz/solution"):
        (suite_dir / rel).mkdir(parents=True, exist_ok=True)
    (suite_dir / "suite.json").write_text(json.dumps(suite_file() if doc is None else doc))
    return suite_dir


class SuiteCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = fixtures.tmpdir(self)

    def assert_rejected(self, suite_dir: Path, fragment: str) -> None:
        with self.assertRaises(suites.SuiteError) as cm:
            suites.load_suite(suite_dir)
        self.assertIn(fragment, str(cm.exception))


class LoadSuiteTests(SuiteCase):
    def test_a_valid_suite_loads_with_its_metadata_and_tasks(self) -> None:
        suite = suites.load_suite(write_suite(self.root))
        (task,) = suite.tasks
        self.assertEqual(
            (suite.name, suite.version, suite.kind, suite.description, task.id, task.tags),
            ("demo", "1.2.3", "capability", "A demo suite.", "fizz", ("bugfix", "python")),
        )
        self.assertEqual(
            (task.provenance.source, task.provenance.license, task.provenance.url), ("synthetic", "MIT", None)
        )

    def test_paths_are_resolved_against_the_suite_directory(self) -> None:
        suite_dir = write_suite(self.root)
        suite = suites.load_suite(suite_dir)
        (task,) = suite.tasks
        root = suite_dir.resolve()
        self.assertEqual(
            (suite.path, task.data["repo"], task.data["hidden_tests"], task.solution),
            (root, str(root / "fizz/repo"), str(root / "fizz/hidden_tests"), root / "fizz/solution"),
        )

    def test_a_task_without_a_solution_has_none(self) -> None:
        entry = task_entry()
        del entry["solution"]
        suite = suites.load_suite(write_suite(self.root, suite_file(tasks=[entry])))
        self.assertIsNone(suite.tasks[0].solution)

    def test_task_keys_the_engine_reads_pass_through_and_the_suite_only_keys_stay_out(self) -> None:
        entry = task_entry(
            ref="main", setup="true", timeout=5, setup_timeout=6, test_timeout=7, protected_paths=["a.py"], grade={}
        )
        entry["graders"] = [{"type": "hidden_tests"}]
        (task,) = suites.load_suite(write_suite(self.root, suite_file(tasks=[entry]))).tasks
        data = dict(task.data)
        self.assertEqual(
            (data["ref"], data["timeout"], data["protected_paths"], data["graders"]),
            ("main", 5, ["a.py"], [{"type": "hidden_tests"}]),
        )
        self.assertFalse({"tags", "solution", "provenance"} & set(data))

    def test_a_git_url_repo_is_kept_as_is_with_its_ref(self) -> None:
        entry = task_entry(repo="https://example.com/x/y.git", ref="v1")
        (task,) = suites.load_suite(write_suite(self.root, suite_file(tasks=[entry]))).tasks
        self.assertEqual((task.data["repo"], task.data["ref"]), ("https://example.com/x/y.git", "v1"))

    def test_a_repo_url_other_than_https_is_rejected(self) -> None:
        """A suite is someone else's data: `file://` reads private local repos, `ssh`/`git@` use the user's keys,
        `ext::` runs a command, and plain `http://` is unauthenticated."""
        for repo in (
            "file:///home/user/private",
            "ssh://git@example.com/x/y.git",
            "git://example.com/x/y.git",
            "http://example.com/x/y.git",
            "git@example.com:x/y.git",
            "ext::sh -c touch% /tmp/pwned",
            "https://",
            "https://example.com/a b",
        ):
            with self.subTest(repo=repo):
                entry = task_entry(repo=repo)
                self.assert_rejected(write_suite(self.root, suite_file(tasks=[entry])), "https://")

    def test_a_regression_suite_and_a_task_without_hidden_tests_load(self) -> None:
        entry = task_entry()
        del entry["hidden_tests"]
        suite = suites.load_suite(write_suite(self.root, suite_file(kind="regression", tasks=[entry])))
        self.assertEqual((suite.kind, "hidden_tests" in suite.tasks[0].data), ("regression", False))

    def test_a_missing_suite_file_is_rejected(self) -> None:
        self.assert_rejected(self.root, "suite.json")

    def test_invalid_json_is_rejected(self) -> None:
        suite_dir = write_suite(self.root)
        (suite_dir / "suite.json").write_text("{nope")
        self.assert_rejected(suite_dir, "not valid JSON")

    def test_a_suite_that_is_not_an_object_is_rejected(self) -> None:
        self.assert_rejected(write_suite(self.root, []), "must be a JSON object")


class SchemaFileTests(SuiteCase):
    def suite_with_schema(self, schema: str | None, ref: str = "s.json") -> Path:
        grader = {"type": "structured_output", "file": "out.json", "schema": ref}
        suite_dir = write_suite(self.root, suite_file(tasks=[task_entry(graders=[grader])]))
        if schema is None:
            (suite_dir / "s.json").unlink(missing_ok=True)
        else:
            (suite_dir / "s.json").write_text(schema)
        return suite_dir

    def test_a_valid_schema_file_loads(self) -> None:
        suites.load_suite(self.suite_with_schema('{"type": "object"}'))

    def test_a_broken_schema_file_is_found_when_the_suite_loads(self) -> None:
        for text, fragment in (
            ("{not json", "Expecting"),
            ('{"type": "string", "pattern": "x"}', "pattern"),
            ("[]", "schema"),
            (None, "cannot be read"),
        ):
            self.assert_rejected(self.suite_with_schema(text), "s.json")
            self.assert_rejected(self.suite_with_schema(text), fragment)

    def test_a_schema_file_that_leaves_the_suite_is_found_when_the_suite_loads(self) -> None:
        self.assert_rejected(self.suite_with_schema(None, ref="../s.json"), "../s.json")

    def test_a_schema_written_in_the_task_is_not_a_file(self) -> None:
        grader = {"type": "structured_output", "file": "out.json", "schema": {"type": "object"}}
        suites.load_suite(write_suite(self.root, suite_file(tasks=[task_entry(graders=[grader])])))


class SuiteFieldTests(SuiteCase):
    def test_an_unknown_top_level_key_is_rejected_by_name(self) -> None:
        self.assert_rejected(write_suite(self.root, suite_file(colour="red")), "colour")

    def test_an_unknown_task_key_is_rejected_by_name(self) -> None:
        self.assert_rejected(write_suite(self.root, suite_file(tasks=[task_entry(shell="rm")])), "shell")

    def test_a_missing_top_level_key_is_rejected_by_name(self) -> None:
        doc = suite_file()
        del doc["kind"]
        self.assert_rejected(write_suite(self.root, doc), "kind")

    def test_a_missing_task_key_is_rejected_by_name(self) -> None:
        entry = task_entry()
        del entry["prompt"]
        self.assert_rejected(write_suite(self.root, suite_file(tasks=[entry])), "prompt")

    def test_a_bad_name_is_rejected(self) -> None:
        for name in ("Demo", "a/b", "a b", "", "../x", 3):
            with self.subTest(name=name):
                self.assert_rejected(write_suite(self.root, suite_file(name=name)), "name")

    def test_a_bad_version_is_rejected(self) -> None:
        for version in ("1.2", "v1.2.3", "", 1):
            with self.subTest(version=version):
                self.assert_rejected(write_suite(self.root, suite_file(version=version)), "version")

    def test_a_bad_kind_is_rejected(self) -> None:
        self.assert_rejected(write_suite(self.root, suite_file(kind="smoke")), "kind")

    def test_another_schema_major_is_rejected_and_a_newer_minor_is_accepted(self) -> None:
        self.assert_rejected(write_suite(self.root, suite_file(schema_version="2.0.0")), "schema_version")
        self.assert_rejected(write_suite(self.root, suite_file(schema_version="nope")), "schema_version")
        suite = suites.load_suite(write_suite(self.root, suite_file(schema_version="1.4.0")))
        self.assertEqual(suite.name, "demo")

    def test_a_bad_description_is_rejected(self) -> None:
        self.assert_rejected(write_suite(self.root, suite_file(description=3)), "description")

    def test_tasks_must_be_a_non_empty_list_of_objects(self) -> None:
        bad: list[Any] = [[], {}, ["x"]]
        for tasks in bad:
            with self.subTest(tasks=tasks):
                self.assert_rejected(write_suite(self.root, suite_file(tasks=tasks)), "tasks")

    def test_a_bad_task_id_is_rejected(self) -> None:
        for task_id in ("A", "a/b", ".x", "", 3):
            with self.subTest(task_id=task_id):
                self.assert_rejected(write_suite(self.root, suite_file(tasks=[task_entry(id=task_id)])), "id")

    def test_duplicate_task_ids_in_one_suite_are_rejected_naming_the_id(self) -> None:
        doc = suite_file(tasks=[task_entry(), task_entry()])
        self.assert_rejected(write_suite(self.root, doc), "duplicate task id 'fizz'")

    def test_wrongly_typed_task_values_are_rejected_by_key(self) -> None:
        for key, value in (
            ("prompt", 3),
            ("test", ["x"]),
            ("setup", 1),
            ("ref", 1),
            ("timeout", "5"),
            ("timeout", True),
            ("timeout", 0),
            ("setup_timeout", -1),
            ("test_timeout", 1.5),
            ("protected_paths", "a"),
            ("protected_paths", [1]),
            ("graders", {}),
            ("grade", []),
            ("tags", "x"),
            ("tags", [1]),
            ("repo", 3),
            ("hidden_tests", 3),
        ):
            with self.subTest(key=key, value=value):
                self.assert_rejected(write_suite(self.root, suite_file(tasks=[task_entry(**{key: value})])), key)

    def test_provenance_must_have_a_source_and_a_license_and_no_other_keys(self) -> None:
        for prov in ("x", {"source": "s"}, {"license": "MIT"}, {"source": 1, "license": "MIT"}):
            with self.subTest(prov=prov):
                self.assert_rejected(
                    write_suite(self.root, suite_file(tasks=[task_entry(provenance=prov)])), "provenance"
                )
        extra = {"source": "s", "license": "MIT", "owner": "x"}
        self.assert_rejected(write_suite(self.root, suite_file(tasks=[task_entry(provenance=extra)])), "owner")
        bad_url = {"source": "s", "license": "MIT", "url": 3}
        self.assert_rejected(write_suite(self.root, suite_file(tasks=[task_entry(provenance=bad_url)])), "url")

    def test_provenance_url_is_optional_and_may_be_a_string(self) -> None:
        prov = {"source": "exercism", "license": "MIT", "url": "https://example.com/x"}
        (task,) = suites.load_suite(write_suite(self.root, suite_file(tasks=[task_entry(provenance=prov)]))).tasks
        self.assertEqual(task.provenance.url, "https://example.com/x")
        no_url = {"source": "synthetic", "license": "MIT"}
        (task,) = suites.load_suite(write_suite(self.root, suite_file(tasks=[task_entry(provenance=no_url)]))).tasks
        self.assertIsNone(task.provenance.url)


class SuitePathTests(SuiteCase):
    def test_a_relative_path_that_escapes_the_suite_directory_is_rejected(self) -> None:
        for key in ("repo", "hidden_tests", "solution"):
            with self.subTest(key=key):
                outside = self.root / "outside"
                outside.mkdir(exist_ok=True)
                entry = task_entry(**{key: "../outside"})
                self.assert_rejected(write_suite(self.root, suite_file(tasks=[entry])), "inside the suite")

    def test_the_etc_style_escape_is_rejected(self) -> None:
        entry = task_entry(repo="../../etc")
        self.assert_rejected(write_suite(self.root, suite_file(tasks=[entry])), "inside the suite")

    def test_an_absolute_path_is_rejected(self) -> None:
        for key in ("repo", "hidden_tests", "solution"):
            with self.subTest(key=key):
                entry = task_entry(**{key: str(self.root)})
                self.assert_rejected(write_suite(self.root, suite_file(tasks=[entry])), "relative")

    def test_a_symlinked_directory_pointing_outside_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        suite_dir = write_suite(self.root, dirs=("fizz/hidden_tests", "fizz/solution"))
        (suite_dir / "fizz" / "repo").symlink_to(outside, target_is_directory=True)
        self.assert_rejected(suite_dir, "inside the suite")

    def test_a_symlink_anywhere_in_a_task_directory_is_rejected(self) -> None:
        """Files and directories, absolute and relative targets, inside and outside the suite, at any depth."""
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        for key in ("repo", "hidden_tests", "solution"):
            suite_dir = write_suite(self.root / key)
            link = suite_dir / "fizz" / key / "sub" / "link"
            link.parent.mkdir()
            targets = {
                "file outside": outside / "secret.txt",
                "dir outside": outside,
                "file inside": suite_dir / "suite.json",
                "dir inside": suite_dir / "fizz",
            }
            for label, target in targets.items():
                for style, value in (("absolute", str(target)), ("relative", os.path.relpath(target, link.parent))):
                    with self.subTest(key=key, target=label, style=style):
                        link.symlink_to(value, target_is_directory=target.is_dir())
                        try:
                            self.assert_rejected(suite_dir, "symlink sub/link")
                        finally:
                            link.unlink()

    def test_a_symlink_to_a_directory_inside_the_task_is_rejected_by_name(self) -> None:
        suite_dir = write_suite(self.root)
        (suite_dir / "fizz" / "repo" / "esc").symlink_to("../..", target_is_directory=True)
        self.assert_rejected(suite_dir, "symlink esc")

    def test_an_absolute_symlink_into_the_hidden_tests_cannot_reach_the_workdir(self) -> None:
        suite_dir = write_suite(self.root)
        (suite_dir / "fizz" / "repo" / "peek").symlink_to(suite_dir / "fizz" / "hidden_tests", target_is_directory=True)
        self.assert_rejected(suite_dir, "symlink peek")

    def test_a_task_directory_reached_through_a_symlinked_parent_is_rejected(self) -> None:
        suite_dir = write_suite(self.root)
        (suite_dir / "alias").symlink_to(suite_dir / "fizz", target_is_directory=True)
        doc = suite_file(tasks=[task_entry(repo="alias/repo")])
        (suite_dir / "suite.json").write_text(json.dumps(doc))
        self.assert_rejected(suite_dir, "symlink")

    def test_a_repo_directory_that_is_a_git_repository_is_rejected(self) -> None:
        for how in (".git", "named.git"):
            with self.subTest(how=how):
                suite_dir = write_suite(self.root / how)
                repo = suite_dir / "fizz" / "repo"
                if how == ".git":
                    (repo / ".git").mkdir()
                    entry = task_entry()
                else:
                    (suite_dir / "fizz" / "x.git").mkdir()
                    entry = task_entry(repo="fizz/x.git")
                (suite_dir / "suite.json").write_text(json.dumps(suite_file(tasks=[entry])))
                self.assert_rejected(suite_dir, "must not be a git repository")

    def test_a_missing_directory_is_rejected_by_key(self) -> None:
        suite_dir = write_suite(self.root, dirs=("fizz/repo", "fizz/hidden_tests"))
        self.assert_rejected(suite_dir, "solution")

    def test_a_path_that_is_a_file_is_rejected(self) -> None:
        suite_dir = write_suite(self.root)
        (suite_dir / "fizz" / "file").write_text("x")
        doc = suite_file(tasks=[task_entry(repo="fizz/file")])
        (suite_dir / "suite.json").write_text(json.dumps(doc))
        self.assert_rejected(suite_dir, "not a directory")


class SuiteTasksTests(SuiteCase):
    def test_engine_tasks_are_prefixed_with_the_suite_name_and_carry_the_suite_keys(self) -> None:
        suite = suites.load_suite(write_suite(self.root))
        (task,) = suites.suite_tasks(suite)
        self.assertEqual(
            (task["id"], task["suite"], task["suite_kind"], task["tags"], task["prompt"]),
            ("demo/fizz", "demo", "capability", ["bugfix", "python"], "fix it"),
        )
        self.assertEqual(task["solution"], str(suite.path / "fizz/solution"))

    def test_a_task_without_a_solution_has_no_solution_key(self) -> None:
        entry = task_entry()
        del entry["solution"]
        suite = suites.load_suite(write_suite(self.root, suite_file(tasks=[entry])))
        self.assertNotIn("solution", suites.suite_tasks(suite)[0])

    def test_the_label_is_name_at_version(self) -> None:
        suite = suites.load_suite(write_suite(self.root))
        self.assertEqual(suites.suite_label(suite), "demo@1.2.3")


class ExampleSuiteTests(unittest.TestCase):
    """The suites shipped in `suites/`: synthetic, offline, and every task checked by running it."""

    def test_every_example_task_fails_untouched_and_passes_with_its_solution(self) -> None:
        with fixtures.quiet() as out:
            cli.main(["suite", "validate", "--run", "--allow-unsandboxed", *map(str, EXAMPLE_SUITES)])
        lines = [line for line in out.getvalue().splitlines() if "/" in line and not line.startswith("ok  ")]
        self.assertEqual(
            sorted(lines),
            [
                "examples-capability/dependency-upgrade: ok",
                "examples-capability/extract-function: ok",
                "examples-capability/path-traversal-fix: ok",
                "regression/fizzbuzz: ok",
            ],
        )

    def test_suite_kinds_and_provenance(self) -> None:
        loaded = {s.name: s for s in map(suites.load_suite, EXAMPLE_SUITES)}
        self.assertEqual(
            {name: (s.kind, s.version) for name, s in loaded.items()},
            {"regression": ("regression", "1.0.0"), "examples-capability": ("capability", "1.0.0")},
        )
        for suite in loaded.values():
            for task in suite.tasks:
                self.assertEqual((task.provenance.source, task.provenance.license), ("synthetic", "MIT"), task.id)
                self.assertIsNotNone(task.solution, task.id)

    def test_the_artifacts_suite_loads_with_its_three_graders_and_every_file_it_names(self) -> None:
        suite = suites.load_suite(SUITES / "examples-artifacts")
        (task,) = suites.suite_tasks(suite)
        self.assertEqual(
            (suite.kind, task["id"], task["grade"]), ("capability", "examples-artifacts/html-report", {"mode": "all"})
        )
        self.assertEqual([g["type"] for g in task["graders"]], ["html_check", "structured_output", "llm_rubric"])
        self.assertEqual(len(grade_plan(task).graders), 3)
        for name in ("rubrics/report-quality.json", "schemas/summary.schema.json"):
            self.assertTrue((suite.path / name).is_file(), name)
        for name in ("report.html", "summary.json"):
            self.assertTrue((Path(task["solution"]) / name).is_file(), name)
        self.assertFalse((Path(task["repo"]) / "report.html").exists())

    def test_every_hidden_test_file_carries_the_contamination_marker(self) -> None:
        files = [p for suite in EXAMPLE_SUITES for p in suite.glob("*/hidden_tests/test_*.py")]
        self.assertEqual(len(files), 4)
        self.assertTrue(all(MARKER in p.read_text() for p in files), files)

    def test_the_example_configs_use_the_example_suites(self) -> None:
        for name in ("config.example.json", "config.litellm.example.json"):
            cfg = json.loads((fixtures.ROOT / name).read_text())
            self.assertEqual(cfg["suites"], ["suites/regression", "suites/examples-capability"], name)
            self.assertNotIn("fizzbuzz", [t["id"] for t in cfg["tasks"]], name)

    def test_the_example_suites_expand_into_engine_tasks_of_a_valid_config(self) -> None:
        import harness_bench as hb

        cfg = hb.expand_suites(
            {"suites": ["suites/regression", "suites/examples-capability"], "tasks": []}, fixtures.ROOT
        )
        hb.validate_config({**cfg, "harnesses": {}})
        self.assertEqual(len(cfg["tasks"]), 4)


if __name__ == "__main__":
    unittest.main()
