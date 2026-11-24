"""Unit tests for the artifact graders (`html_check`, `structured_output`) and the path rule they share."""

import json
import os
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
from hbench.graders import _files
from hbench.graders._files import read_artifact, resolve_artifact
from hbench.graders._options import MAX_VIOLATIONS
from hbench.graders.base import GradeContext
from hbench.graders.html_check import HtmlCheckGrader
from hbench.graders.registry import grader_for
from hbench.graders.structured_output import StructuredOutputGrader


def no_run(command: str, timeout: int, log_name: str) -> tuple[int | None, bool, str, str]:
    raise AssertionError("an artifact grader never runs a command")


def context(workdir: Path, base: Path | None = None, task: Mapping[str, Any] | None = None) -> GradeContext:
    logged: list[str] = []
    ctx = GradeContext(
        workdir=workdir, task=task or {}, base=base or workdir, changes=(), run=no_run, log=logged.append
    )
    return ctx


GOOD_PAGE = (
    "<!doctype html><html><head><title>t</title><link rel='stylesheet' href='style.css'></head>"
    "<body><h1 id='top' class='big'>Hi</h1><p>a<br>b<img src='logo.png'></p>"
    "<a href='#top'>up</a><a href='https://example.com/x'>remote</a><a href='sub/page.html?x=1#f'>sub</a>"
    "</body></html>"
)


def page_dir(test: unittest.TestCase, html: str = GOOD_PAGE) -> Path:
    work = fixtures.tmpdir(test)
    (work / "report.html").write_text(html)
    (work / "style.css").write_text("body{}")
    (work / "logo.png").write_bytes(b"png")
    (work / "sub").mkdir()
    (work / "sub" / "page.html").write_text("<p>x</p>")
    return work


class ResolveArtifactTests(unittest.TestCase):
    def test_a_file_inside_the_workdir_resolves(self) -> None:
        work = page_dir(self)
        self.assertEqual(resolve_artifact(work, "sub/page.html"), work / "sub" / "page.html")

    def test_escapes_and_bad_names_are_refused(self) -> None:
        work = page_dir(self)
        for bad in ("../x", "sub/../../x", "/etc/passwd", "", "..", "a\0b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                resolve_artifact(work, bad)

    def test_a_symlink_on_the_way_or_as_the_file_is_refused(self) -> None:
        work, outside = page_dir(self), fixtures.tmpdir(self)
        (outside / "secret.txt").write_text("host secret")
        (work / "dirlink").symlink_to(outside, target_is_directory=True)
        (work / "filelink.txt").symlink_to(outside / "secret.txt")
        for bad in ("dirlink/secret.txt", "filelink.txt"):
            with self.assertRaises(ValueError, msg=bad):
                resolve_artifact(work, bad)

    def test_a_missing_file_a_directory_and_a_hard_link_are_refused_when_read(self) -> None:
        work, outside = page_dir(self), fixtures.tmpdir(self)
        (outside / "secret.txt").write_text("host secret")
        os.link(outside / "secret.txt", work / "hard.txt")
        for bad in ("nope.txt", "sub", "hard.txt"):
            with self.assertRaises(ValueError, msg=bad):
                read_artifact(work, bad)

    def test_a_fifo_is_refused_without_blocking(self) -> None:
        work = page_dir(self)
        os.mkfifo(work / "pipe")
        with self.assertRaises(ValueError):
            read_artifact(work, "pipe")

    def test_a_fifo_swapped_in_after_the_path_check_is_refused_without_blocking(self) -> None:
        work = page_dir(self)
        os.mkfifo(work / "pipe")
        with mock.patch.object(_files, "resolve_artifact", return_value=work / "pipe"):
            with self.assertRaises(ValueError) as cm:
                read_artifact(work, "pipe")
        self.assertIn("not a regular file", str(cm.exception))

    def test_a_symlink_swapped_in_after_the_path_check_is_not_followed(self) -> None:
        work, outside = page_dir(self), fixtures.tmpdir(self)
        (outside / "secret.txt").write_text("host secret")
        (work / "swapped.txt").symlink_to(outside / "secret.txt")
        with mock.patch.object(_files, "resolve_artifact", return_value=work / "swapped.txt"):
            with self.assertRaises(ValueError):
                read_artifact(work, "swapped.txt")

    def test_a_parent_directory_swapped_for_a_symlink_after_the_path_check_is_refused(self) -> None:
        work, outside = page_dir(self), fixtures.tmpdir(self)
        (outside / "page.html").write_text("host secret")
        (work / "swapped").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(_files, "resolve_artifact", return_value=work / "swapped" / "page.html"):
            with self.assertRaises(ValueError) as cm:
                read_artifact(work, "swapped/page.html")
        self.assertIn("cannot be read", str(cm.exception))
        # a real swap: the check passes on a directory, then the directory becomes a link before the open
        (work / "sub2").mkdir()
        (work / "sub2" / "page.html").write_text("ok")
        real = _files.resolve_artifact

        def swap_after_check(root: Path, rel: str) -> Path:
            path = real(root, rel)
            (work / "sub2" / "page.html").unlink()
            (work / "sub2").rmdir()
            (work / "sub2").symlink_to(outside, target_is_directory=True)
            return path

        with mock.patch.object(_files, "resolve_artifact", swap_after_check):
            with self.assertRaises(ValueError):
                read_artifact(work, "sub2/page.html")

    def test_a_nested_file_and_an_empty_relative_path(self) -> None:
        work = page_dir(self)
        self.assertEqual(read_artifact(work, "sub/page.html"), "<p>x</p>")
        with mock.patch.object(_files, "resolve_artifact", return_value=work):
            with self.assertRaises(ValueError):
                read_artifact(work, ".")

    def test_a_hard_link_is_refused_by_the_read_itself(self) -> None:
        work, outside = page_dir(self), fixtures.tmpdir(self)
        (outside / "secret.txt").write_text("host secret")
        os.link(outside / "secret.txt", work / "hard.txt")
        with mock.patch.object(_files, "artifact_file", side_effect=AssertionError("the read must not rely on it")):
            with self.assertRaises(ValueError) as cm:
                read_artifact(work, "hard.txt")
        self.assertIn("hard link", str(cm.exception))

    def test_the_size_limit_is_exact_and_a_file_that_grows_after_the_check_is_cut_off(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "f.txt").write_text("x" * 50)
        self.assertEqual(len(read_artifact(work, "f.txt", limit=50)), 50)
        (work / "f.txt").write_text("x" * 51)
        with self.assertRaises(ValueError):
            read_artifact(work, "f.txt", limit=50)
        with mock.patch.object(os, "fdopen", side_effect=AssertionError("an oversized file is not read")):
            with self.assertRaises(ValueError) as cm:
                read_artifact(work, "f.txt", limit=50)
        self.assertIn("larger than 50", str(cm.exception))
        real = os.fstat(os.open(work / "f.txt", os.O_RDONLY))
        small = os.stat_result((*real[:6], 0, *real[7:10]))  # what fstat said before the file grew
        with mock.patch.object(os, "fstat", return_value=small), self.assertRaises(ValueError):
            read_artifact(work, "f.txt", limit=50)

    def test_a_read_error_is_refused(self) -> None:
        work = page_dir(self)
        with mock.patch.object(os, "fdopen", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(ValueError) as cm:
                read_artifact(work, "report.html")
        self.assertIn("cannot be read", str(cm.exception))

    def test_an_unreadable_file_is_refused(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "locked.txt").write_text("x")
        (work / "locked.txt").chmod(0)
        self.addCleanup((work / "locked.txt").chmod, 0o600)
        if os.access(work / "locked.txt", os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        with self.assertRaises(ValueError):
            read_artifact(work, "locked.txt")

    def test_a_file_over_the_limit_is_refused_and_a_binary_file_is_decoded_leniently(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "big.txt").write_text("x" * 100)
        with self.assertRaises(ValueError):
            read_artifact(work, "big.txt", limit=50)
        (work / "bin.txt").write_bytes(b"ok\xff")
        self.assertTrue(read_artifact(work, "bin.txt").startswith("ok"))


class HtmlCheckTests(unittest.TestCase):
    def grade(self, work: Path, **cfg: Any) -> tuple[bool, list[str]]:
        grader = HtmlCheckGrader({"type": "html_check", "file": "report.html", **cfg})
        ctx = context(work)
        result = grader.grade(ctx)
        self.assertEqual((result.grader, result.grader_version), ("html_check", "1"))
        self.assertEqual(result.score, 1.0 if result.passed else 0.0)
        return result.passed, grader.violations(ctx)

    def test_a_valid_page_passes(self) -> None:
        work = page_dir(self)
        passed, found = self.grade(work, required=["h1", "#top", ".big", "h1#top", "h1.big", "p"])
        self.assertEqual((passed, found), (True, []))

    def test_each_missing_required_selector_fails(self) -> None:
        work = page_dir(self)
        for selector in ("table", "#nope", ".nope", "h2#top", "p.big"):
            passed, found = self.grade(work, required=[selector])
            self.assertFalse(passed, selector)
            self.assertEqual(len(found), 1, selector)
            self.assertIn(selector, found[0])

    def test_external_scripts_fail_by_default_and_are_allowed_on_request(self) -> None:
        for src in (
            "https://cdn.example.com/x.js",
            "//cdn.example.com/x.js",
            "HTTP://x/y.js",
            "data:text/javascript,1",
        ):
            work = page_dir(self, f"<html><body><script src='{src}'></script></body></html>")
            self.assertFalse(self.grade(work)[0], src)
            self.assertTrue(self.grade(work, forbid_external_scripts=False, check_local_links=False)[0], src)

    def test_a_local_script_and_an_inline_script_are_fine(self) -> None:
        work = page_dir(
            self, "<html><body><script src='style.css'></script><script>var a = '<b>';</script></body></html>"
        )
        self.assertEqual(self.grade(work), (True, []))

    def test_a_broken_relative_link_fails_and_remote_links_are_not_fetched(self) -> None:
        work = page_dir(self, "<html><body><a href='missing.html'>x</a><img src='https://e.com/i.png'></body></html>")
        passed, found = self.grade(work)
        self.assertFalse(passed)
        self.assertEqual(len(found), 1)
        self.assertIn("missing.html", found[0])
        self.assertTrue(self.grade(work, check_local_links=False)[0])

    def test_a_link_that_leaves_the_workdir_or_goes_through_a_symlink_is_broken(self) -> None:
        outside = fixtures.tmpdir(self)
        (outside / "secret.txt").write_text("host secret")
        for href in ("../secret.txt", "/etc/passwd", "link/secret.txt", "linkfile"):
            work = page_dir(self, f"<html><body><a href='{href}'>x</a></body></html>")
            (work / "link").symlink_to(outside, target_is_directory=True)
            (work / "linkfile").symlink_to(outside / "secret.txt")
            self.assertFalse(self.grade(work)[0], href)

    def test_links_resolve_next_to_the_html_file_and_are_percent_decoded(self) -> None:
        work = page_dir(self, "<html><body><a href='b%20c.html'>x</a><a href='../report.html'>up</a></body></html>")
        (work / "sub" / "b c.html").write_text("x")
        (work / "sub" / "inner.html").write_text(
            "<html><body><a href='b%20c.html'>x</a><a href='../report.html'>y</a></body></html>"
        )
        grader = HtmlCheckGrader({"type": "html_check", "file": "sub/inner.html"})
        self.assertTrue(grader.grade(context(work)).passed)

    def test_unclosed_and_mismatched_elements_fail(self) -> None:
        for html in (
            "<html><body><div><p>x</p></body></html>",
            "<html><body><div><b>x</div></b></body></html>",
            "<html><body></span></body></html>",
            "<html><body><p>x</body>",
        ):
            passed, found = self.grade(page_dir(self, html), check_local_links=False)
            self.assertFalse(passed, html)
            self.assertTrue(found, html)

    def test_void_and_self_closing_elements_are_not_unclosed(self) -> None:
        html = "<html><body><br><hr/><img src='logo.png'><input type='text'><div/></body></html>"
        self.assertEqual(self.grade(page_dir(self, html)), (True, []))

    def test_a_stray_end_tag_of_a_void_element_is_ignored(self) -> None:
        self.assertEqual(self.grade(page_dir(self, "<html><body>a</br>b</body></html>")), (True, []))

    def test_file_escapes_fail_the_grader(self) -> None:
        work, outside = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (outside / "x.html").write_text(GOOD_PAGE)
        (work / "link.html").symlink_to(outside / "x.html")
        (work / "dir").symlink_to(outside, target_is_directory=True)
        for name in ("../x.html", "/etc/hostname", "link.html", "dir/x.html", "missing.html"):
            passed, found = self.grade(work, file=name)
            self.assertFalse(passed, name)
            self.assertEqual(len(found), 1, name)

    def test_the_violations_are_written_to_the_grader_log(self) -> None:
        work = page_dir(self, "<html><body><div></body></html>")
        logged: list[str] = []
        ctx = GradeContext(workdir=work, task={}, base=work, changes=(), run=no_run, log=logged.append)
        HtmlCheckGrader({"file": "report.html", "check_local_links": False}).grade(ctx)
        self.assertIn("<div> is still open", "".join(logged))

    def test_bad_options_are_config_errors(self) -> None:
        for cfg in (
            {},
            {"file": ""},
            {"file": 3},
            {"file": "a.html", "required": "h1"},
            {"file": "a.html", "required": [3]},
            {"file": "a.html", "required": ["div p"]},
            {"file": "a.html", "required": [""]},
            {"file": "a.html", "forbid_external_scripts": "yes"},
            {"file": "a.html", "check_local_links": 1},
            {"file": "a.html", "typo": True},
        ):
            with self.assertRaises(ValueError, msg=repr(cfg)):
                HtmlCheckGrader(cfg)

    def test_a_protocol_relative_script_is_reported_as_external(self) -> None:
        work = page_dir(self, "<html><body><script src='//cdn/x.js'></script></body></html>")
        passed, found = self.grade(work, check_local_links=False)
        self.assertFalse(passed)
        self.assertEqual(found, ["external script '//cdn/x.js'"])

    def test_a_class_selector_matches_whole_class_names_only(self) -> None:
        work = page_dir(self, "<html><body><p class='abc'>x</p></body></html>")
        self.assertFalse(self.grade(work, required=[".a"])[0])
        self.assertTrue(self.grade(work, required=[".abc"])[0])

    def test_a_link_with_no_path_is_not_a_file_link(self) -> None:
        work = page_dir(self, "<html><body><a href='?page=2'>next</a><a href='?'>x</a></body></html>")
        self.assertEqual(self.grade(work), (True, []))

    def test_a_directory_link_is_fine_when_the_directory_exists_in_the_workdir(self) -> None:
        outside = fixtures.tmpdir(self)
        html = "<html><body><a href='sub/'>a</a><a href='./sub/?x=1'>b</a><a href='.'>c</a></body></html>"
        work = page_dir(self, html)
        self.assertEqual(self.grade(work), (True, []))
        for href in ("nope/", "link/", "../"):
            work = page_dir(self, f"<html><body><a href='{href}'>x</a></body></html>")
            (work / "link").symlink_to(outside, target_is_directory=True)
            self.assertFalse(self.grade(work)[0], href)

    def test_a_link_to_a_fifo_or_a_hard_link_is_broken(self) -> None:
        outside = fixtures.tmpdir(self)
        (outside / "secret.txt").write_text("host secret")
        work = page_dir(self, "<html><body><a href='pipe'>x</a><a href='hard.txt'>y</a></body></html>")
        os.mkfifo(work / "pipe")
        os.link(outside / "secret.txt", work / "hard.txt")
        passed, found = self.grade(work)
        self.assertFalse(passed)
        self.assertEqual(found, ["broken local link 'pipe'", "broken local link 'hard.txt'"])

    def test_a_page_of_unmatched_tags_is_capped_and_fast(self) -> None:
        # 1 MB of nested opening tags, then unmatched end tags, then classes and links: it used to take minutes.
        html = "<a>" * 100_000 + "</b>" * 100_000 + "<i class='c1 c2 c3'>" * 20_000 + "<img src='x%d'>" * 5_000
        html += "<p>" * 30_000
        self.assertGreater(len(html), 1_000_000)
        work = page_dir(self, html)
        start = time.monotonic()
        passed, found = self.grade(work, required=[".c3", ".nope"])
        self.assertLess(time.monotonic() - start, 30)  # generous: coverage and slow CI; the old code took hours
        self.assertFalse(passed)
        self.assertEqual(len(found), MAX_VIOLATIONS + 1)
        self.assertRegex(found[-1], r"^… \d+ more$")

    def test_end_tags_of_deeply_nested_elements_close_in_linear_time(self) -> None:
        depth = 100_000
        html = "<div>" * depth + "</div>" * depth
        start = time.monotonic()
        self.assertEqual(self.grade(page_dir(self, html), check_local_links=False), (True, []))
        self.assertLess(time.monotonic() - start, 30)  # generous: coverage and slow CI; the old code took hours

    def test_many_broken_links_make_capped_violations_and_one_log_write(self) -> None:
        html = "<html><body>" + "".join(f"<a href='missing{i}.html'>x</a>" for i in range(20_000)) + "</body></html>"
        work = page_dir(self, html)
        logged: list[str] = []
        ctx = GradeContext(workdir=work, task={}, base=work, changes=(), run=no_run, log=logged.append)
        start = time.monotonic()
        result = HtmlCheckGrader({"file": "report.html"}).grade(ctx)
        self.assertLess(time.monotonic() - start, 30)  # generous: coverage and slow CI; the old code took hours
        self.assertFalse(result.passed)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0].count("\n"), MAX_VIOLATIONS + 1)
        self.assertTrue(logged[0].endswith("… more checks skipped\n"))

    def test_checks_stop_once_the_violations_are_full(self) -> None:
        links = "".join(f"<a href='missing{i}.html'>x</a>" for i in range(5_000))
        scripts = "".join(f"<script src='https://e.com/{i}.js'></script>" for i in range(5_000))
        work = page_dir(self, "<html><body>" + links + scripts + "</body></html>")
        with mock.patch("hbench.graders.html_check.artifact_file", side_effect=ValueError("gone")) as lookup:
            passed, found = self.grade(work)
        self.assertFalse(passed)
        self.assertLessEqual(lookup.call_count, 2 * (MAX_VIOLATIONS + 1))
        self.assertEqual((len(found), found[-1]), (MAX_VIOLATIONS + 1, "… more checks skipped"))
        # only scripts: the cap applies to them too
        work = page_dir(self, "<html><body>" + scripts + "</body></html>")
        passed, found = self.grade(work, check_local_links=False)
        self.assertEqual((len(found), found[-1]), (MAX_VIOLATIONS + 1, "… more checks skipped"))

    def test_many_elements_are_matched_in_linear_time_and_keep_no_attribute_dicts(self) -> None:
        body = "".join(f"<i id='i{i}' class='c{i} d{i}' data-x='{'y' * 50}'></i>" for i in range(60_000))
        work = page_dir(self, "<html><body>" + body + "</body></html>")
        selectors = [f".c{i}" for i in range(0, 60_000, 6)] + [
            "#i59999",
            "i#i5",
            "i.d7",
            "b",
            "#nope",
            ".nope",
            "i.nope",
        ]
        start = time.monotonic()
        passed, found = self.grade(work, required=selectors, check_local_links=False)
        self.assertLess(time.monotonic() - start, 30)
        self.assertFalse(passed)
        self.assertEqual(found, [f"no element matches {s!r}" for s in ("b", "#nope", ".nope", "i.nope")])

    def test_a_repeated_broken_link_is_looked_up_once(self) -> None:
        work = page_dir(self, "<html><body>" + "<a href='gone.html'>x</a>" * 5 + "</body></html>")
        with mock.patch("hbench.graders.html_check.artifact_file", side_effect=ValueError("gone")) as lookup:
            passed, found = self.grade(work)
        self.assertFalse(passed)
        self.assertEqual(len(found), 5)
        self.assertEqual(lookup.call_count, 2)  # one lookup for `grade`, one for `violations`

    def test_the_registry_knows_the_grader_and_allows_type_and_weight(self) -> None:
        grader = grader_for({"type": "html_check", "file": "a.html", "weight": 2})
        self.assertIsInstance(grader, HtmlCheckGrader)


class StructuredOutputTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "total": {"type": "integer", "minimum": 0},
            "rows": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["total"],
        "additionalProperties": False,
    }

    def setup(self, document: str, schema: Any = None) -> tuple[Path, StructuredOutputGrader]:
        work = fixtures.tmpdir(self)
        (work / "summary.json").write_text(document)
        grader = StructuredOutputGrader(
            {"type": "structured_output", "file": "summary.json", "schema": schema or self.SCHEMA}
        )
        return work, grader

    def test_valid_json_passes(self) -> None:
        work, grader = self.setup('{"total": 3, "rows": ["a"]}')
        result = grader.grade(context(work))
        self.assertEqual(
            (result.grader, result.grader_version, result.passed, result.score), ("structured_output", "1", True, 1.0)
        )

    def test_invalid_json_and_schema_violations_fail(self) -> None:
        for document in (
            "{not json",
            '{"total": -1}',
            '{"rows": []}',
            '{"total": 1, "extra": 1}',
            "[]",
            '{"total": NaN}',
        ):
            work, grader = self.setup(document)
            result = grader.grade(context(work))
            self.assertEqual((result.passed, result.score), (False, 0.0), document)
            self.assertTrue(grader.violations(context(work)), document)

    def test_nan_and_infinity_are_not_json_even_where_the_schema_would_allow_them(self) -> None:
        for document in ('{"a": NaN}', '{"a": Infinity}', "[-Infinity]"):
            work, grader = self.setup(document, schema={"type": ["object", "array"]})
            found = grader.violations(context(work))
            self.assertEqual(len(found), 1, document)
            self.assertIn("is not valid JSON", found[0])

    def test_schema_errors_are_capped(self) -> None:
        work, grader = self.setup(json.dumps({"rows": [1] * 5000, "total": 1}))
        logged: list[str] = []
        ctx = GradeContext(workdir=work, task={}, base=work, changes=(), run=no_run, log=logged.append)
        self.assertFalse(grader.grade(ctx).passed)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0].count("\n"), MAX_VIOLATIONS + 1)
        self.assertTrue(logged[0].endswith("… 4000 more\n"))

    def test_a_document_nested_too_deep_fails_instead_of_crashing(self) -> None:
        work, grader = self.setup("[" * 100_000)
        self.assertFalse(grader.grade(context(work)).passed)

    def test_the_schema_may_be_a_file_next_to_the_suite(self) -> None:
        base = fixtures.tmpdir(self)
        (base / "schemas").mkdir()
        (base / "schemas" / "s.json").write_text(json.dumps(self.SCHEMA))
        work = fixtures.tmpdir(self)
        (work / "summary.json").write_text('{"total": 1}')
        grader = StructuredOutputGrader({"file": "summary.json", "schema": "schemas/s.json"})
        self.assertTrue(grader.grade(context(work, base)).passed)
        (work / "summary.json").write_text('{"total": "x"}')
        self.assertFalse(grader.grade(context(work, base)).passed)

    def test_the_suite_dir_of_a_suite_task_is_where_the_schema_file_lives(self) -> None:
        suite, work = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (suite / "s.json").write_text(json.dumps(self.SCHEMA))
        (work / "summary.json").write_text('{"total": 1}')
        grader = StructuredOutputGrader({"file": "summary.json", "schema": "s.json"})
        ctx = context(work, base=fixtures.tmpdir(self), task={"suite_dir": str(suite)})
        self.assertTrue(grader.grade(ctx).passed)

    def test_a_schema_file_that_is_missing_unreadable_or_escaping_fails_the_grader(self) -> None:
        base, work = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (work / "summary.json").write_text("{}")
        (base / "bad.json").write_text("{not json")
        (base / "unsupported.json").write_text('{"type": "string", "pattern": "x"}')
        (base / "list.json").write_text("[]")
        for ref in ("missing.json", "bad.json", "unsupported.json", "list.json", "../outside.json"):
            grader = StructuredOutputGrader({"file": "summary.json", "schema": ref})
            self.assertFalse(grader.grade(context(work, base)).passed, ref)

    def test_a_schema_keyword_the_validator_does_not_support_is_a_config_error(self) -> None:
        for schema in (
            {"type": "string", "pattern": "x"},
            {"type": "object", "properties": {"a": {"type": "string", "format": "uri"}}},
            {"type": "array", "items": {"minLength": 1}},
            {"$defs": {"a": {"oneOf": []}}, "type": "object"},
            {"type": "color"},
        ):
            with self.assertRaises(ValueError, msg=repr(schema)):
                StructuredOutputGrader({"file": "a.json", "schema": schema})

    def test_file_escapes_fail_the_grader(self) -> None:
        work, outside = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (outside / "x.json").write_text('{"total": 1}')
        (work / "link.json").symlink_to(outside / "x.json")
        for name in ("../x.json", "/etc/hostname", "link.json", "missing.json"):
            grader = StructuredOutputGrader({"file": name, "schema": self.SCHEMA})
            self.assertFalse(grader.grade(context(work)).passed, name)

    def test_bad_options_are_config_errors(self) -> None:
        bad_configs: list[dict[str, Any]] = [
            {},
            {"file": "a.json"},
            {"schema": {}},
            {"file": "", "schema": {}},
            {"file": "a.json", "schema": 3},
            {
                "file": "a.json",
                "schema": "",
            },
            {"file": "a.json", "schema": {}, "typo": 1},
        ]
        for cfg in bad_configs:
            with self.assertRaises(ValueError, msg=repr(cfg)):
                StructuredOutputGrader(cfg)


if __name__ == "__main__":
    unittest.main()
