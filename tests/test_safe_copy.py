"""Tests for `hbench.safe_copy.safe_copy_into`: a copy onto a tree the agent controls never writes through a link."""

import os
import shutil
import unittest
from pathlib import Path

import fixtures
from hbench.safe_copy import safe_copy_into


class SafeCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        root = fixtures.tmpdir(self)
        self.src, self.dest, self.outside = root / "src", root / "dest", root / "outside"
        for path in (self.src, self.dest, self.outside):
            path.mkdir()
        self.canary = self.outside / "canary.txt"
        self.canary.write_text("host file")

    def test_it_copies_files_and_empty_directories_over_existing_ones(self) -> None:
        (self.src / "a" / "b").mkdir(parents=True)
        (self.src / "a" / "b" / "f.txt").write_text("new")
        (self.src / "empty").mkdir()
        (self.dest / "a").mkdir()
        (self.dest / "a" / "b").mkdir()
        (self.dest / "a" / "b" / "f.txt").write_text("old")
        safe_copy_into(self.src, self.dest)
        self.assertEqual(((self.dest / "a" / "b" / "f.txt").read_text(), (self.dest / "empty").is_dir()), ("new", True))

    def test_a_destination_file_that_is_a_symlink_is_replaced_and_its_target_is_untouched(self) -> None:
        (self.src / "test_hidden.py").write_text("hidden")
        (self.dest / "test_hidden.py").symlink_to(self.canary)
        safe_copy_into(self.src, self.dest)
        written = self.dest / "test_hidden.py"
        self.assertEqual(
            (written.is_symlink(), written.read_text(), self.canary.read_text()), (False, "hidden", "host file")
        )

    def test_a_destination_directory_that_is_a_symlink_is_replaced_and_its_target_is_untouched(self) -> None:
        (self.src / "esc").mkdir()
        (self.src / "esc" / "PWNED").write_text("x")
        (self.dest / "esc").symlink_to(self.outside, target_is_directory=True)
        safe_copy_into(self.src, self.dest)
        self.assertEqual(
            (
                (self.dest / "esc").is_symlink(),
                (self.dest / "esc" / "PWNED").exists(),
                sorted(os.listdir(self.outside)),
            ),
            (False, True, ["canary.txt"]),
        )

    def test_a_symlink_two_levels_up_the_destination_path_is_replaced(self) -> None:
        (self.src / "a" / "b").mkdir(parents=True)
        (self.src / "a" / "b" / "f.txt").write_text("x")
        (self.dest / "a").symlink_to(self.outside, target_is_directory=True)
        safe_copy_into(self.src, self.dest)
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])
        self.assertTrue((self.dest / "a" / "b" / "f.txt").is_file())

    def test_a_destination_hard_linked_to_a_host_file_is_replaced_not_written_through(self) -> None:
        (self.src / "test_hidden.py").write_text("hidden")
        os.link(self.canary, self.dest / "test_hidden.py")
        safe_copy_into(self.src, self.dest)
        self.assertEqual((self.canary.read_text(), (self.dest / "test_hidden.py").read_text()), ("host file", "hidden"))

    def test_a_directory_where_a_file_goes_and_a_file_where_a_directory_goes_are_replaced(self) -> None:
        (self.src / "f").write_text("file")
        (self.src / "d").mkdir()
        (self.src / "d" / "g").write_text("g")
        (self.dest / "f").mkdir()
        (self.dest / "f" / "junk").write_text("x")
        (self.dest / "d").write_text("not a directory")
        safe_copy_into(self.src, self.dest)
        self.assertEqual(((self.dest / "f").read_text(), (self.dest / "d" / "g").read_text()), ("file", "g"))

    def test_ignored_names_are_not_copied(self) -> None:
        (self.src / "__pycache__").mkdir()
        (self.src / "__pycache__" / "x.pyc").write_text("x")
        (self.src / "keep.py").write_text("k")
        safe_copy_into(self.src, self.dest, ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(sorted(os.listdir(self.dest)), ["keep.py"])

    def test_ignored_files_are_not_copied_either(self) -> None:
        (self.src / "skip.pyc").write_text("x")
        (self.src / "keep.py").write_text("k")
        safe_copy_into(self.src, self.dest, ignore=shutil.ignore_patterns("*.pyc"))
        self.assertEqual(sorted(os.listdir(self.dest)), ["keep.py"])

    def test_a_fifo_or_socket_at_the_destination_is_replaced(self) -> None:
        import socket

        (self.src / "a").write_text("a")
        (self.src / "b").write_text("b")
        os.mkfifo(self.dest / "a")
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.dest / "b"))
            safe_copy_into(self.src, self.dest)
        self.assertEqual(((self.dest / "a").read_text(), (self.dest / "b").read_text()), ("a", "b"))

    def test_a_fifo_where_a_directory_goes_is_replaced(self) -> None:
        (self.src / "d").mkdir()
        (self.src / "d" / "f").write_text("f")
        os.mkfifo(self.dest / "d")
        safe_copy_into(self.src, self.dest)
        self.assertEqual((self.dest / "d" / "f").read_text(), "f")

    def test_a_destination_root_that_is_a_symlink_is_refused(self) -> None:
        (self.src / "f").write_text("x")
        link = self.dest.parent / "link"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            safe_copy_into(self.src, link)
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])

    def test_a_source_symlink_is_copied_as_its_content(self) -> None:
        (self.src / "real.txt").write_text("content")
        (self.src / "alias.txt").symlink_to(self.src / "real.txt")
        safe_copy_into(self.src, self.dest)
        alias = self.dest / "alias.txt"
        self.assertEqual((alias.is_symlink(), alias.read_text()), (False, "content"))

    def test_the_destination_root_is_created_when_missing(self) -> None:
        (self.src / "f").write_text("x")
        target = Path(self.dest / "new" / "root")
        safe_copy_into(self.src, target)
        self.assertTrue((target / "f").is_file())


if __name__ == "__main__":
    unittest.main()
