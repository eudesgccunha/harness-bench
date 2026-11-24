"""Grader for an HTML artifact: required elements, no external scripts, local links that resolve, well-formed tags.

It parses with the standard library only and never fetches anything: a remote link is left alone."""

import posixpath
import re
from collections import Counter
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from hbench.core.models import GraderResult
from hbench.graders._files import artifact_file, read_artifact, resolve_artifact
from hbench.graders._options import MAX_VIOLATIONS, bool_option, reject_unknown, string_option
from hbench.graders.base import GradeContext

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_SELECTOR = re.compile(r"([A-Za-z][A-Za-z0-9-]*)?(?:#([A-Za-z0-9_-]+)|\.([A-Za-z0-9_-]+))?")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class _Found:
    """The violations of one page, at most `MAX_VIOLATIONS` of them; `lines()` ends with how many were dropped."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._dropped = 0
        self._cut = False

    @property
    def full(self) -> bool:
        return len(self._items) >= MAX_VIOLATIONS

    def add(self, message: str) -> None:
        if self.full:
            self._dropped += 1
        else:
            self._items.append(message)

    def cut(self) -> None:
        """Note that checks were skipped because the list is full."""
        self._cut = True

    def lines(self) -> list[str]:
        if self._dropped:
            return [*self._items, f"… {self._dropped} more"]
        return [*self._items, "… more checks skipped"] if self._cut else list(self._items)


class _Page(HTMLParser):
    """Collects what the checks need from one document, in one pass and without keeping the elements: the tags,
    ids and classes seen (for `required`), the `src` of every `<script>`, every `href` and `src`, and the
    well-formedness problems. Memory is the size of what is distinct plus the links."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.tag_ids: set[tuple[str, str]] = set()
        self.tag_classes: set[tuple[str, str]] = set()
        self.script_srcs: list[str] = []
        self.links: list[str] = []
        self.problems = _Found()
        self._open: list[str] = []
        self._open_count: Counter[str] = Counter()  # how many of each tag are open: O(1) membership

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}  # a value-less attribute is an empty string
        self.tags.add(tag)
        if "id" in values:
            self.ids.add(values["id"])
            self.tag_ids.add((tag, values["id"]))
        for name in values.get("class", "").split():
            self.classes.add(name)
            self.tag_classes.add((tag, name))
        if tag == "script":
            self.script_srcs.append(values.get("src", "").strip())
        self.links += [url for key in ("href", "src") if (url := values.get(key, "").strip())]

    def matches(self, selector: tuple[str | None, str | None, str | None]) -> bool:
        """Whether some element matches the simple selector `(tag, id, class)`."""
        tag, element_id, class_name = selector
        if tag is not None and element_id is not None:
            return (tag, element_id) in self.tag_ids
        if tag is not None and class_name is not None:
            return (tag, class_name) in self.tag_classes
        if tag is not None:
            return tag in self.tags
        return (element_id in self.ids) if element_id is not None else class_name in self.classes

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)
        if tag not in _VOID:
            self._open.append(tag)
            self._open_count[tag] += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if not self._open_count[tag]:
            self.problems.add(f"unexpected </{tag}>")
            return
        if self._open[-1] != tag:
            self.problems.add(f"</{tag}> closes while <{self._open[-1]}> is still open")
        while True:
            popped = self._open.pop()
            self._open_count[popped] -= 1
            if popped == tag:
                break

    def finish(self) -> None:
        self.close()
        for tag in self._open:
            self.problems.add(f"unclosed <{tag}>")


def _selector(text: str) -> tuple[str | None, str | None, str | None]:
    """`(tag, id, class)` of a simple selector: `tag`, `#id`, `.class`, `tag#id` or `tag.class`."""
    match = _SELECTOR.fullmatch(text) if isinstance(text, str) else None
    if match is None or not any(match.groups()):
        raise ValueError(f"html_check: unsupported selector {text!r}: use tag, #id, .class, tag#id or tag.class")
    tag = match.group(1)
    return (tag.lower() if tag else None), match.group(2), match.group(3)


def _is_remote(url: str) -> bool:
    return url.startswith("//") or _SCHEME.match(url) is not None


class HtmlCheckGrader:
    """Options: `file` (the HTML file in the workdir), `required` (simple selectors that must each match an
    element), `forbid_external_scripts` (default true: a `<script src>` with a scheme or `//` fails) and
    `check_local_links` (default true: a relative `href` or `src` must name an existing file inside the workdir).
    Unclosed and mismatched non-void elements always fail. Only the violations' names go to the grader log."""

    name = "html_check"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        reject_unknown(cfg, {"file", "required", "forbid_external_scripts", "check_local_links"}, self.name)
        self._file = string_option(cfg, "file", self.name)
        required = cfg.get("required", [])
        if not isinstance(required, list):
            raise ValueError(f"{self.name}: 'required' must be a list of selectors")
        self._required = [(text, _selector(text)) for text in required]
        self._forbid_scripts = bool_option(cfg, "forbid_external_scripts", True, self.name)
        self._check_links = bool_option(cfg, "check_local_links", True, self.name)

    def violations(self, ctx: GradeContext) -> list[str]:
        """One message per broken rule; a file that cannot be read is one violation."""
        try:
            text = read_artifact(ctx.workdir, self._file)
        except ValueError as e:
            return [str(e)]
        page = _Page()
        page.feed(text)
        page.finish()
        found = page.problems
        for raw, sel in self._required:
            if not page.matches(sel):
                found.add(f"no element matches {raw!r}")
        known: dict[str, bool] = {}  # link -> resolves, so a page that repeats a link looks it up once
        if self._forbid_scripts:
            for src in page.script_srcs:
                if found.full:
                    found.cut()
                    break
                if _is_remote(src):
                    found.add(f"external script {src!r}")
        if self._check_links:
            for url in page.links:
                if found.full:  # the grader already fails: do not look up the rest
                    found.cut()
                    break
                if not url.startswith("#") and not _is_remote(url):
                    if url not in known:
                        known[url] = self._resolves(ctx, url)
                    if not known[url]:
                        found.add(f"broken local link {url!r}")
        return found.lines()

    def _resolves(self, ctx: GradeContext, url: str) -> bool:
        """Whether the relative `url` names a file (or, for `dir/`, a directory) of the workdir; a link with no
        path (`?page=2`) always resolves."""
        link = unquote(urlsplit(url).path)
        if not link:
            return True
        rel = posixpath.normpath(posixpath.join(PurePosixPath(self._file).parent.as_posix(), link))
        try:
            if resolve_artifact(ctx.workdir, rel).is_dir():
                return True
            artifact_file(ctx.workdir, rel)
        except ValueError:
            return False
        return True

    def grade(self, ctx: GradeContext) -> GraderResult:
        found = self.violations(ctx)
        ctx.log("".join(message + "\n" for message in found))
        passed = not found
        return GraderResult(grader=self.name, grader_version=self.version, passed=passed, score=1.0 if passed else 0.0)
