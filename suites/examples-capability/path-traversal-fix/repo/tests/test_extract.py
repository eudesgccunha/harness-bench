import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_tools.extract import extract_all


class TestExtract(unittest.TestCase):
    def test_files_and_directories_are_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("docs/readme.txt", "hello")
                zf.writestr("empty/", "")
            dest = Path(tmp) / "out"
            written = extract_all(archive, dest)
            self.assertEqual(written, [dest / "docs" / "readme.txt"])
            self.assertEqual((dest / "docs" / "readme.txt").read_text(), "hello")
            self.assertTrue((dest / "empty").is_dir())


if __name__ == "__main__":
    unittest.main()
