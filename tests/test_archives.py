import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import winfonts_engine as winfonts


class ArchiveTests(unittest.TestCase):
    def test_parse_and_select_windows_image_payload(self) -> None:
        entries = winfonts.parse_7z_slt(
            """\
Path = sources/install.wim
Folder = -
Size = 1234

Path = support/large.bin
Folder = -
Size = 999999

"""
        )
        selected, mode = winfonts.select_archive_entries(entries)
        self.assertEqual([entry.path for entry in selected], ["sources/install.wim"])
        self.assertEqual(mode, "selected Windows image payload")

    def test_select_office_streams_and_loose_fonts(self) -> None:
        entries = [
            winfonts.ArchiveEntry("Office/Data/16.0/stream.x64.x-none.dat", 100, False),
            winfonts.ArchiveEntry("Office/Data/16.0/huge.unneeded", 5000, False),
            winfonts.ArchiveEntry("compatibility/font.otc", 50, False),
        ]
        selected, mode = winfonts.select_archive_entries(entries)
        self.assertEqual(
            {entry.path for entry in selected},
            {
                "Office/Data/16.0/stream.x64.x-none.dat",
                "compatibility/font.otc",
            },
        )
        self.assertEqual(mode, "selected Office Click-to-Run payload")

    def test_archive_listing_rejects_path_traversal(self) -> None:
        result = subprocess.CompletedProcess(
            ["7z"],
            0,
            "Path = ../escape.ttf\nFolder = -\nSize = 10\n\n",
            "",
        )
        with (
            tempfile.TemporaryDirectory() as raw,
            patch("winfonts_engine.subprocess.run", return_value=result),
        ):
            with self.assertRaises(winfonts.WinfontsError) as raised:
                winfonts.list_archive_entries("7z", Path(raw) / "media.iso")
        self.assertEqual(raised.exception.code, winfonts.EXIT_IO)

    def test_extraction_checks_free_space_before_running_7z(self) -> None:
        manager = winfonts.TempManager()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "Windows.iso"
            source.write_bytes(b"image")
            with (
                patch("winfonts_engine.archive_extractor", return_value="7z"),
                patch(
                    "winfonts_engine.list_archive_entries",
                    return_value=[
                        winfonts.ArchiveEntry(
                            "sources/install.wim",
                            1024 * 1024 * 1024,
                            False,
                        )
                    ],
                ),
                patch(
                    "winfonts_engine.shutil.disk_usage",
                    return_value=shutil._ntuple_diskusage(
                        2 * 1024 * 1024 * 1024,
                        2 * 1024 * 1024 * 1024 - 1024,
                        1024,
                    ),
                ),
                patch("winfonts_engine.subprocess.run") as runner,
            ):
                with self.assertRaises(winfonts.WinfontsError) as raised:
                    manager.extract_image(source, root)
        self.assertEqual(raised.exception.code, winfonts.EXIT_IO)
        runner.assert_not_called()

    @unittest.skipUnless(winfonts.archive_extractor(), "7-Zip is not installed")
    def test_real_7z_fallback_extracts_only_selected_payload(self) -> None:
        extractor = winfonts.archive_extractor()
        assert extractor is not None
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = root / "payload"
            (payload / "sources").mkdir(parents=True)
            (payload / "sources/install.wim").write_bytes(b"selected")
            (payload / "unneeded.bin").write_bytes(b"not selected")
            image = root / "Windows.iso"
            subprocess.run(
                [extractor, "a", "-tzip", str(image), "."],
                cwd=payload,
                text=True,
                capture_output=True,
                check=True,
            )
            manager = winfonts.TempManager()
            with redirect_stderr(io.StringIO()):
                extracted = manager.extract_image(image, root)

            self.assertEqual(
                (extracted / "sources/install.wim").read_bytes(),
                b"selected",
            )
            self.assertFalse((extracted / "unneeded.bin").exists())


if __name__ == "__main__":
    unittest.main()
