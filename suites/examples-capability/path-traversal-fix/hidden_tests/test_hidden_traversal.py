# HIDDEN-TEST-MARKER: the contamination probe looks for this line outside the benchmark.
import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_tools.extract import UnsafeArchiveError, extract_all


def make_archive(root: Path, members: dict[str, str]) -> Path:
    archive = root / "upload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return archive


class TestTraversal(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.dest = self.root / "out"

    def assert_rejected(self, members: dict[str, str]) -> None:
        archive = make_archive(self.root, members)
        with self.assertRaises(UnsafeArchiveError):
            extract_all(archive, self.dest)

    def test_a_safe_archive_still_extracts(self) -> None:
        archive = make_archive(self.root, {"a/b.txt": "x", "c.txt": "y"})
        written = extract_all(archive, self.dest)
        self.assertEqual(sorted(p.relative_to(self.dest).as_posix() for p in written), ["a/b.txt", "c.txt"])

    def test_dot_dot_member_is_rejected_and_writes_nothing_outside(self) -> None:
        self.assert_rejected({"ok.txt": "fine", "../escape.txt": "pwned"})
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse((self.dest / "ok.txt").exists(), "a safe member written before the unsafe one was found")

    def test_an_absolute_member_is_rejected_and_writes_nothing(self) -> None:
        absolute = self.root / "absolute-escape" / "x.txt"
        self.assert_rejected({"ok.txt": "fine", str(absolute): "pwned"})
        self.assertFalse(absolute.exists())
        self.assertFalse((self.dest / "ok.txt").exists(), "a safe member written before the unsafe one was found")

    def test_dot_dot_after_a_directory_is_rejected(self) -> None:
        self.assert_rejected({"a/../../escape.txt": "pwned"})
        self.assertFalse((self.root / "escape.txt").exists())

    def test_a_sibling_directory_sharing_the_name_prefix_is_rejected(self) -> None:
        self.assert_rejected({"../out-evil/x.txt": "pwned"})
        self.assertFalse((self.root / "out-evil").exists())

    def test_dot_dot_that_stays_inside_is_allowed(self) -> None:
        archive = make_archive(self.root, {"a/../b.txt": "x"})
        written = extract_all(archive, self.dest)
        self.assertEqual([p.relative_to(self.dest).as_posix() for p in written], ["b.txt"])

    def test_the_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(UnsafeArchiveError, ValueError))


if __name__ == "__main__":
    unittest.main()
