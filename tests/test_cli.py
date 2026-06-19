import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import winfonts_engine as winfonts


class CommandLineTests(unittest.TestCase):
    def test_new_commands_and_aliases_parse(self) -> None:
        parser = winfonts.build_parser()
        for argv, expected in (
            (["interactive"], winfonts.interactive_command),
            (["menu"], winfonts.interactive_command),
            (["wizard"], winfonts.interactive_command),
            (["list"], winfonts.list_command),
            (["installed"], winfonts.list_command),
            (["help", "install"], winfonts.help_command),
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertIs(args.func, expected)

    def test_interactive_mode_can_exit_cleanly(self) -> None:
        output = io.StringIO()
        with patch("builtins.input", return_value="q"), redirect_stdout(output):
            code = winfonts.interactive_command(argparse.Namespace())
        self.assertEqual(code, winfonts.EXIT_OK)
        self.assertIn("interactive mode", output.getvalue())
        self.assertIn("Goodbye.", output.getvalue())

    def test_interactive_argument_error_returns_to_menu_caller(self) -> None:
        parser = winfonts.build_parser()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = winfonts.run_interactive_action(
                parser,
                ["install", "/tmp/source", "--image", "not-a-number"],
            )
        self.assertEqual(code, winfonts.EXIT_USAGE)

    def test_list_json_for_missing_manifest_has_no_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "missing-parent" / "manifest.jsonl"
            output = io.StringIO()
            args = argparse.Namespace(manifest=str(manifest), json=True)
            with redirect_stdout(output):
                code = winfonts.list_command(args)
            payload = json.loads(output.getvalue())
            self.assertEqual(code, winfonts.EXIT_OK)
            self.assertEqual(payload["fonts"], [])
            self.assertFalse(manifest.parent.exists())

    def test_status_json_for_missing_manifest_has_no_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "missing-parent" / "manifest.jsonl"
            output = io.StringIO()
            args = argparse.Namespace(manifest=str(manifest), json=True)
            with redirect_stdout(output):
                code = winfonts.status_command(args)
            payload = json.loads(output.getvalue())
            self.assertEqual(code, winfonts.EXIT_OK)
            self.assertFalse(payload["exists"])
            self.assertFalse(manifest.parent.exists())

    def test_missing_managed_file_is_a_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest.jsonl"
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": "a" * 64,
                "dest_path": str(root / "missing.ttf"),
                "installed_filename": "missing.ttf",
                "faces": [],
            }
            manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
            args = argparse.Namespace(manifest=str(manifest), json=False)
            with redirect_stdout(io.StringIO()):
                code = winfonts.status_command(args)
            self.assertEqual(code, winfonts.EXIT_VERIFY)

    def test_record_display_name_prefers_internal_full_name(self) -> None:
        record = {
            "installed_filename": "opaque.ttf",
            "faces": [
                {
                    "family": "Aptos",
                    "style": "Bold",
                    "fullname": "Aptos Bold",
                }
            ],
        }
        self.assertEqual(winfonts.record_display_name(record), "Aptos Bold")

    def test_list_prefers_stable_media_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            font = root / "font.ttf"
            font.write_bytes(b"font")
            manifest = root / "manifest.jsonl"
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": winfonts.sha256_file(font),
                "dest_path": str(font),
                "installed_filename": font.name,
                "source_path": "/tmp/ephemeral-extraction",
                "source_media_path": "/home/me/Windows.iso",
                "faces": [],
            }
            manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
            output = io.StringIO()
            args = argparse.Namespace(manifest=str(manifest), json=True)
            with redirect_stdout(output):
                code = winfonts.list_command(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, winfonts.EXIT_OK)
        self.assertEqual(payload["fonts"][0]["source"], "/home/me/Windows.iso")

    def test_source_sha256_requires_a_full_digest(self) -> None:
        parser = winfonts.build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["scan", "/tmp/source", "--source-sha256", "abc"])

        digest = "A" * 64
        args = parser.parse_args(["scan", "/tmp/source", "--source-sha256", digest])
        self.assertEqual(args.source_sha256, digest.casefold())

    def test_source_sha256_mismatch_stops_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "media.iso"
            source.write_bytes(b"test media")
            args = argparse.Namespace(
                sources=[str(source)],
                dest=None,
                manifest=None,
                image=None,
                source_sha256="0" * 64,
            )
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(winfonts.WinfontsError) as raised:
                    winfonts.install_command(args)
        self.assertEqual(raised.exception.code, winfonts.EXIT_VERIFY)

    def test_disk_image_falls_back_to_archive_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "Office.iso"
            source.write_bytes(b"not mounted in this unit test")
            extracted = root / "extracted"
            extracted.mkdir()
            (extracted / "font.ttf").write_bytes(b"font")
            manager = winfonts.TempManager()
            mount_error = winfonts.WinfontsError("mount denied", winfonts.EXIT_USAGE)
            with (
                patch.object(manager, "mount_image", side_effect=mount_error),
                patch.object(manager, "extract_image", return_value=extracted),
                patch("winfonts_engine.archive_extractor", return_value="7z"),
            ):
                info = winfonts.detect_source(source, root, manager)
        self.assertEqual(info.source_type, "iso-image-archive:loose-font-directory")

    def test_legacy_office_media_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "setup.exe").write_bytes(b"")
            (root / "Office64WW.msi").write_bytes(b"")
            manager = winfonts.TempManager()
            info = winfonts.detect_source(root, root, manager)
        self.assertEqual(info.source_type, "office-legacy-media")

    def test_legacy_office_uses_loose_fonts_without_archive_tools(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "output"
            output.mkdir()
            (root / "Office64WW.msi").write_bytes(b"")
            loose = root / "compatibility.ttf"
            loose.write_bytes(b"font")
            info = winfonts.SourceInfo("office-legacy-media", root=root)
            with (
                patch("winfonts_engine.command_exists", return_value=False),
                redirect_stderr(io.StringIO()),
            ):
                candidates = winfonts.extract_legacy_office_candidates(info, output)
        self.assertEqual([candidate.path for candidate in candidates], [loose.resolve()])


if __name__ == "__main__":
    unittest.main()
