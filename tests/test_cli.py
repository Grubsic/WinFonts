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


if __name__ == "__main__":
    unittest.main()
