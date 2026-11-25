"""Tests for the task importers (ADR 0013): the Exercism importer on a synthetic tree and the plugin registry."""

import json
import os
import unittest
from pathlib import Path
from typing import Any

import fixtures
from hbench import cli, importers
from hbench.importers import ImporterError, ImportRequest
from hbench.plugins import IMPORTER_GROUP, PluginError
from hbench.suites import load_suite, suite_tasks
from test_plugins import installed

PRACTICE = Path("exercises") / "practice"
LICENSE_TEXT = "Synthetic license text for the importer tests.\n"
URL = "https://example.com/exercism/python"

TWO_FER_STUB = "def two_fer(name=None):\n    pass\n"
TWO_FER_SOLUTION = "def two_fer(name=None):\n    return f'One for {name or \"you\"}, one for me.'\n"
TWO_FER_TEST = (
    "import unittest\n"
    "from two_fer import two_fer\n\n\n"
    "class TwoFerTest(unittest.TestCase):\n"
    "    def test_no_name(self):\n"
    "        self.assertEqual(two_fer(), 'One for you, one for me.')\n"
    "    def test_name(self):\n"
    "        self.assertEqual(two_fer('Bob'), 'One for Bob, one for me.')\n"
)
LEAP_STUB = "def leap_year(year):\n    pass\n"
LEAP_SOLUTION = "def leap_year(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"
LEAP_TEST = (
    "import unittest\n"
    "from leap import leap_year\n\n\n"
    "class LeapTest(unittest.TestCase):\n"
    "    def test_leap(self):\n"
    "        self.assertTrue(leap_year(2000))\n"
    "    def test_not_leap(self):\n"
    "        self.assertFalse(leap_year(1900))"  # no final newline on purpose
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_exercise(
    source: Path,
    slug: str,
    stub: tuple[str, str],
    test: tuple[str, str],
    solution: str,
    *,
    append: str | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    """One exercise in the Exercism Python layout; `stub` and `test` are `(file name, content)`."""
    root = source / PRACTICE / slug
    write(root / stub[0], stub[1])
    write(root / test[0], test[1])
    write(root / ".meta" / "example.py", solution)
    write(root / ".docs" / "instructions.md", f"# {slug}\n\nSolve {slug}.\n")
    if append is not None:
        write(root / ".docs" / "instructions.append.md", append)
    document = config or {"files": {"solution": [stub[0]], "test": [test[0]], "example": [".meta/example.py"]}}
    write(root / ".meta" / "config.json", json.dumps(document))
    return root


def make_source(base: Path) -> Path:
    """A synthetic checkout with a LICENSE and two exercises, written at runtime (no third-party content)."""
    source = base / "source"
    write(source / "LICENSE", LICENSE_TEXT)
    make_exercise(
        source,
        "two-fer",
        ("two_fer.py", TWO_FER_STUB),
        ("two_fer_test.py", TWO_FER_TEST),
        TWO_FER_SOLUTION,
        append="Extra note.\n",
    )
    make_exercise(source, "leap", ("leap.py", LEAP_STUB), ("leap_test.py", LEAP_TEST), LEAP_SOLUTION)
    return source


class ImporterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        self.source = make_source(self.base)
        self.out = self.base / "out" / "pack"
        self.out.parent.mkdir()

    def request(self, **overrides: Any) -> ImportRequest:
        values: dict[str, Any] = {
            "source": self.source,
            "out": self.out,
            "name": "exercism-python",
            "version": "1.0.0",
            "license": "MIT",
            "source_url": URL,
            "exercises": (),
        }
        values.update(overrides)
        return ImportRequest(**values)

    def refused(self, message: str, **overrides: Any) -> None:
        with self.assertRaises(ImporterError) as cm:
            importers.run_import("exercism", self.request(**overrides))
        self.assertIn(message, str(cm.exception))
        self.assertFalse(os.path.lexists(self.out), "a failed import leaves nothing behind")


class ExercismImportTests(ImporterTestCase):
    def test_the_import_writes_a_suite_that_loads(self) -> None:
        suite = importers.run_import("exercism", self.request())
        self.assertEqual((suite.name, suite.version, suite.kind), ("exercism-python", "1.0.0", "capability"))
        self.assertEqual([t.id for t in suite.tasks], ["leap", "two-fer"])
        self.assertEqual(load_suite(self.out).tasks, suite.tasks)

    def test_every_task_carries_its_provenance_and_the_license_file_is_copied(self) -> None:
        suite = importers.run_import("exercism", self.request(license="MIT"))
        for task in suite.tasks:
            self.assertEqual(
                (task.provenance.source, task.provenance.license, task.provenance.url),
                ("exercism/python", "MIT", f"{URL}/exercises/practice/{task.id}"),
            )
        self.assertEqual((self.out / "LICENSE.exercism-python").read_text(), LICENSE_TEXT)

    def test_the_trailing_slash_of_the_source_url_is_dropped(self) -> None:
        suite = importers.run_import("exercism", self.request(source_url=URL + "/"))
        self.assertEqual(suite.tasks[0].provenance.url, f"{URL}/exercises/practice/leap")

    def test_repo_hidden_tests_and_solution_come_from_the_right_files(self) -> None:
        importers.run_import("exercism", self.request())
        task = self.out / "two-fer"
        self.assertEqual((task / "repo" / "two_fer.py").read_text(), TWO_FER_STUB)
        self.assertEqual((task / "solution" / "two_fer.py").read_text(), TWO_FER_SOLUTION)
        self.assertEqual(sorted(p.name for p in (task / "repo").iterdir()), ["two_fer.py"])
        self.assertEqual(sorted(p.name for p in (task / "hidden_tests").iterdir()), ["two_fer_test.py"])

    def test_every_hidden_test_file_has_the_marker_on_its_own_line(self) -> None:
        importers.run_import("exercism", self.request())
        for name, test in (("two-fer", "two_fer_test.py"), ("leap", "leap_test.py")):
            lines = (self.out / name / "hidden_tests" / test).read_text().splitlines()
            self.assertEqual(lines[-1], "# HIDDEN-TEST-MARKER")
        self.assertTrue(
            (self.out / "two-fer" / "hidden_tests" / "two_fer_test.py").read_text().startswith(TWO_FER_TEST)
        )

    def test_the_task_has_a_test_command_and_a_prompt_with_the_instructions(self) -> None:
        suite = importers.run_import("exercism", self.request())
        by_id = {t.id: t for t in suite.tasks}
        two_fer = by_id["two-fer"].data
        self.assertEqual(two_fer["test"], "python3 -m unittest -q two_fer_test.py")
        self.assertIn("Solve two-fer.", two_fer["prompt"])
        self.assertIn("Extra note.", two_fer["prompt"])
        self.assertIn("two_fer.py", two_fer["prompt"])
        self.assertNotIn("Extra note.", by_id["leap"].data["prompt"])

    def test_exercise_selects_a_subset_in_the_given_order_of_the_suite(self) -> None:
        suite = importers.run_import("exercism", self.request(exercises=("two-fer",)))
        self.assertEqual([t.id for t in suite.tasks], ["two-fer"])
        self.assertFalse((self.out / "leap").exists())

    def test_the_imported_suite_passes_validate_run(self) -> None:
        importers.run_import("exercism", self.request())
        with fixtures.quiet() as out:
            cli.main(["suite", "validate", "--run", "--allow-unsandboxed", str(self.out)])
        self.assertEqual(out.getvalue().count("exercism-python/"), 2)
        self.assertIn("exercism-python/leap: ok", out.getvalue())
        self.assertIn("exercism-python/two-fer: ok", out.getvalue())
        self.assertEqual(len(suite_tasks(load_suite(self.out))), 2)

    def test_a_task_with_several_test_and_solution_files(self) -> None:
        config = {
            "files": {
                "solution": ["a.py", "pkg/b.py"],
                "test": ["a_test.py", "tests/b_test.py"],
                "example": [".meta/example.py", ".meta/example_b.py"],
            }
        }
        root = make_exercise(
            self.source, "multi", ("a.py", "A = 0\n"), ("a_test.py", "# t\n"), "A = 1\n", config=config
        )
        write(root / "pkg" / "b.py", "B = 0\n")
        write(root / "tests" / "b_test.py", "# t2\n")
        write(root / ".meta" / "example_b.py", "B = 1\n")
        suite = importers.run_import("exercism", self.request(exercises=("multi",)))
        self.assertEqual(suite.tasks[0].data["test"], "python3 -m unittest -q a_test.py tests/b_test.py")
        self.assertEqual((self.out / "multi" / "solution" / "pkg" / "b.py").read_text(), "B = 1\n")
        self.assertTrue((self.out / "multi" / "hidden_tests" / "tests" / "b_test.py").read_text().endswith("MARKER\n"))


class ExercismRefusalTests(ImporterTestCase):
    def exercise(self, slug: str = "leap") -> Path:
        return self.source / PRACTICE / slug

    def set_config(self, document: object, slug: str = "leap") -> None:
        (self.exercise(slug) / ".meta" / "config.json").write_text(json.dumps(document))

    def test_a_source_without_a_license_file_is_refused(self) -> None:
        (self.source / "LICENSE").unlink()
        self.refused("LICENSE")

    def test_a_license_that_is_a_symlink_is_refused(self) -> None:
        (self.source / "LICENSE").unlink()
        (self.source / "LICENSE").symlink_to(self.base / "elsewhere")
        self.refused("LICENSE")

    def test_a_symlinked_practice_directory_is_refused(self) -> None:
        moved = self.base / "moved"
        (self.source / "exercises").rename(moved)
        (self.source / "exercises").symlink_to(moved)
        self.refused("symlink")

    def test_the_same_file_listed_twice_is_refused(self) -> None:
        files = {"solution": ["leap.py", "leap.py"], "test": ["leap_test.py"], "example": [".meta/example.py"] * 2}
        self.set_config({"files": files})
        self.refused("duplicate", exercises=("leap",))

    def test_a_source_that_is_not_a_directory_is_refused(self) -> None:
        self.refused("not a directory", source=self.base / "nope")

    def test_an_existing_output_is_refused_and_left_alone(self) -> None:
        self.out.mkdir()
        (self.out / "keep.txt").write_text("keep")
        with self.assertRaises(ImporterError) as cm:
            importers.run_import("exercism", self.request())
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual((self.out / "keep.txt").read_text(), "keep")

    def test_an_output_in_a_missing_directory_is_refused(self) -> None:
        with self.assertRaises(ImporterError) as cm:
            importers.run_import("exercism", self.request(out=self.base / "missing" / "pack"))
        self.assertIn("cannot create", str(cm.exception))

    def test_an_output_that_is_a_dangling_symlink_is_refused(self) -> None:
        self.out.symlink_to(self.base / "nowhere")
        with self.assertRaises(ImporterError):
            importers.run_import("exercism", self.request())
        self.assertTrue(self.out.is_symlink())

    def test_a_file_symlink_in_the_exercise_is_refused(self) -> None:
        (self.exercise() / "extra.py").symlink_to(self.base / "elsewhere")
        self.refused("symlink")

    def test_a_directory_symlink_in_the_exercise_is_refused(self) -> None:
        (self.exercise() / "linkdir").symlink_to(self.base)
        self.refused("symlink")

    def test_a_symlink_as_the_exercise_directory_is_refused(self) -> None:
        (self.source / PRACTICE / "linked").symlink_to(self.exercise())
        self.refused("symlink", exercises=("linked",))

    def test_a_symlink_among_the_discovered_exercises_is_refused(self) -> None:
        (self.source / PRACTICE / "linked").symlink_to(self.exercise())
        self.refused("symlink")

    def test_dotdot_and_absolute_names_in_config_are_refused(self) -> None:
        for bad in ("../outside.py", "/etc/passwd", "a/../../b.py", ""):
            for key in ("solution", "test", "example"):
                files = {"solution": ["leap.py"], "test": ["leap_test.py"], "example": [".meta/example.py"]}
                files[key] = [bad]
                self.set_config({"files": files})
                with self.subTest(bad=bad, key=key):
                    self.refused("", exercises=("leap",))

    def test_a_file_over_the_size_limit_is_refused(self) -> None:
        (self.exercise() / "leap_test.py").write_text("x" * (importers.exercism.MAX_FILE_BYTES + 1))
        self.refused("larger than")

    def test_a_bad_slug_is_refused(self) -> None:
        for slug in ("../x", "Bad_Slug", "a b", "", "/abs"):
            with self.subTest(slug=slug):
                self.refused("slug", exercises=(slug,))
        (self.source / PRACTICE / "Bad_Slug").mkdir()
        self.refused("slug")

    def test_an_unknown_exercise_is_refused(self) -> None:
        self.refused("not in the source", exercises=("missing",))

    def test_a_source_without_exercises_is_refused(self) -> None:
        (self.base / "empty" / "exercises" / "practice").mkdir(parents=True)
        write(self.base / "empty" / "LICENSE", LICENSE_TEXT)
        self.refused("no exercises", source=self.base / "empty")
        self.refused("no practice directory", source=self.base / "source" / "exercises" / "practice" / "leap")

    def test_a_non_https_source_url_is_refused(self) -> None:
        for url in ("http://example.com/x", "git@github.com:a/b", "file:///x", "https://", "ftp://x", ""):
            with self.subTest(url=url):
                self.refused("https://", source_url=url)

    def test_bad_names_versions_and_licenses_are_refused(self) -> None:
        self.refused("name", name="Bad Name")
        self.refused("name", name="../up")
        self.refused("version", version="1.0")
        self.refused("license", license="")
        self.refused("license", license="MIT\nX")

    def test_a_config_that_is_not_valid_is_refused(self) -> None:
        documents: list[Any] = [
            [],
            {"files": []},
            {"files": {"solution": ["a.py"]}},
            {"files": {"solution": "a.py", "test": [], "example": []}},
        ]
        for document in documents:
            self.set_config(document)
            with self.subTest(document=document):
                self.refused("config.json", exercises=("leap",))
        (self.exercise() / ".meta" / "config.json").write_text("{not json")
        self.refused("config.json", exercises=("leap",))
        (self.exercise() / ".meta" / "config.json").unlink()
        self.refused("config.json", exercises=("leap",))

    def test_example_and_solution_lists_must_pair_up(self) -> None:
        self.set_config({"files": {"solution": ["leap.py"], "test": ["leap_test.py"], "example": []}})
        self.refused("example", exercises=("leap",))

    def test_a_task_needs_a_solution_and_a_test_file(self) -> None:
        self.set_config({"files": {"solution": [], "test": ["leap_test.py"], "example": []}})
        self.refused("solution", exercises=("leap",))
        self.set_config({"files": {"solution": ["leap.py"], "test": [], "example": [".meta/example.py"]}})
        self.refused("test", exercises=("leap",))

    def test_a_test_file_name_that_could_change_the_command_is_refused(self) -> None:
        for name in ("-x.py", "a b_test.py", "a;b_test.py", "$(x)_test.py"):
            write(self.exercise() / name, "# t\n")
            self.set_config({"files": {"solution": ["leap.py"], "test": [name], "example": [".meta/example.py"]}})
            with self.subTest(name=name):
                self.refused("test file name", exercises=("leap",))

    def test_missing_instructions_or_listed_files_are_refused(self) -> None:
        (self.exercise() / ".docs" / "instructions.md").unlink()
        self.refused("instructions.md")
        write(self.exercise() / ".docs" / "instructions.md", "# x\n")
        (self.exercise() / "leap.py").unlink()
        self.refused("leap.py")


class RegistryTests(ImporterTestCase):
    def test_the_exercism_importer_is_built_in(self) -> None:
        self.assertIn("exercism", importers.IMPORTERS)

    def test_an_unknown_importer_names_the_group_and_the_built_ins(self) -> None:
        with self.assertRaises(PluginError) as cm:
            importers.run_import("terminal-bench", self.request())
        self.assertIn(IMPORTER_GROUP, str(cm.exception))
        self.assertIn("exercism", str(cm.exception))

    def test_a_plugin_importer_runs_and_its_result_is_validated(self) -> None:
        module = """
import json
from pathlib import Path


def importer_factory(cfg):
    def run(request):
        repo = request.out / "one" / "repo"
        repo.mkdir(parents=True)
        (repo / "x.py").write_text("x = 1\\n")
        provenance = {"source": "demo", "license": request.license}
        task = {"id": "one", "repo": "one/repo", "prompt": "p", "provenance": provenance}
        document = {"schema_version": "1.0.0", "name": request.name, "version": request.version,
                    "kind": "capability", "description": "demo", "tasks": [task]}
        (request.out / "suite.json").write_text(json.dumps(document))
    return run


def not_callable(cfg):
    return 3


def breaks(cfg):
    def run(request):
        (request.out / "partial.txt").write_text("x")
        raise RuntimeError("boom")
    return run


def writes_nothing(cfg):
    return lambda request: None
"""
        ep = f"[{IMPORTER_GROUP}]\ndemo = demo_plugin:importer_factory\nbad = demo_plugin:not_callable\n"
        ep += "breaks = demo_plugin:breaks\nempty = demo_plugin:writes_nothing\n"
        with installed(self, ep, module), fixtures.quiet():
            suite = importers.run_import("demo", self.request(name="demo-pack"))
            self.assertEqual((suite.name, [t.id for t in suite.tasks]), ("demo-pack", ["one"]))
            self.out = self.out.parent / "second"
            with self.assertRaises(PluginError) as cm:
                importers.run_import("bad", self.request())
            self.assertIn("not a callable", str(cm.exception))
            self.assertFalse(self.out.exists())
            with self.assertRaises(ImporterError) as cm2:
                importers.run_import("breaks", self.request())
            self.assertIn("boom", str(cm2.exception))
            self.assertFalse(self.out.exists())
            with self.assertRaises(ImporterError):
                importers.run_import("empty", self.request())
            self.assertFalse(self.out.exists())

    def test_a_plugin_cannot_take_a_built_in_name(self) -> None:
        ep = f"[{IMPORTER_GROUP}]\nexercism = demo_plugin:importer_factory\n"
        with installed(self, ep, "def importer_factory(cfg):\n    return lambda request: None\n"):
            with self.assertRaises(PluginError) as cm:
                importers.run_import("other", self.request())
        self.assertIn("built-in name", str(cm.exception))


class ImportCommandTests(ImporterTestCase):
    def argv(self, *extra: str) -> list[str]:
        return [
            "suite",
            "import",
            "exercism",
            "--source",
            str(self.source),
            "--out",
            str(self.out),
            "--name",
            "exercism-python",
            "--version",
            "1.0.0",
            "--license",
            "MIT",
            "--source-url",
            URL,
            *extra,
        ]

    def test_the_command_imports_and_says_how_to_validate(self) -> None:
        with fixtures.quiet() as out:
            cli.main(self.argv("--exercise", "leap"))
        self.assertIn("imported 1 task into", out.getvalue())
        self.assertIn("suite validate --run --allow-unsandboxed", out.getvalue())
        self.assertEqual([t.id for t in load_suite(self.out).tasks], ["leap"])

    def test_several_exercises_may_be_given(self) -> None:
        with fixtures.quiet() as out:
            cli.main(self.argv("--exercise", "leap", "two-fer"))
        self.assertIn("imported 2 tasks", out.getvalue())

    def test_errors_exit_with_a_message_and_leave_nothing(self) -> None:
        (self.source / "LICENSE").unlink()
        with self.assertRaises(SystemExit) as cm, fixtures.quiet():
            cli.main(self.argv())
        self.assertIn("suite import: ", str(cm.exception.code))
        self.assertFalse(self.out.exists())

    def test_an_unknown_importer_exits_with_a_message(self) -> None:
        argv = self.argv()
        argv[2] = "nope"
        with self.assertRaises(SystemExit) as cm, fixtures.quiet():
            cli.main(argv)
        self.assertIn("unknown importer", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
