import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import winfonts_engine as winfonts


def install_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "image": None,
        "office_neutral_only": False,
        "office_arch": "x64",
        "office_language": [],
        "duplicate_policy": "install-source",
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def candidate(source: Path, dest: Path) -> winfonts.Candidate:
    item = winfonts.Candidate(
        path=source / "font.ttf",
        original_filename="font.ttf",
        source_type="loose-font-directory",
        source_path=str(source),
        size=4,
        sha256="a" * 64,
        faces=[],
    )
    item.install_dir = dest
    return item


class TransactionTests(unittest.TestCase):
    def test_lock_refuses_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            protected = root / "protected"
            protected.write_text("unchanged", encoding="utf-8")
            lock_path = root / ".lock"
            lock_path.symlink_to(protected)

            with self.assertRaises(winfonts.WinfontsError):
                with winfonts.Lock(lock_path):
                    pass

            self.assertEqual(protected.read_text(encoding="utf-8"), "unchanged")

    def test_install_refuses_a_manifest_with_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "font.ttf").write_bytes(b"candidate")
            dest = root / "fonts"
            manifest = root / "manifest.jsonl"
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": "a" * 64,
                "dest_path": str(dest / "font.ttf"),
                "install_dir": str(dest),
                "newly_created": True,
                "faces": [],
            }
            original = json.dumps(record) + "\n"
            manifest.write_text(original, encoding="utf-8")
            args = argparse.Namespace(
                sources=[str(source)],
                dest=str(dest),
                manifest=str(manifest),
                image=None,
                source_sha256=None,
                duplicate_policy="keep-all",
                dry_run=False,
                office_neutral_only=False,
                office_arch="x64",
                office_language=[],
            )

            with patch.object(winfonts, "detect_source") as detect:
                with self.assertRaises(winfonts.WinfontsError) as raised:
                    winfonts.install_command(args)

            self.assertEqual(raised.exception.code, winfonts.EXIT_VERIFY)
            self.assertIn("repair --dry-run", str(raised.exception))
            self.assertEqual(manifest.read_text(encoding="utf-8"), original)
            detect.assert_not_called()

    def test_repair_drops_missing_and_compacts_duplicate_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest.jsonl"
            missing = root / "missing.ttf"
            records = [
                {
                    "schema": winfonts.SCHEMA,
                    "record": "transaction",
                    "transaction_id": "old",
                    "state": "complete",
                },
                {
                    "schema": winfonts.SCHEMA,
                    "record": "font_file",
                    "transaction_id": "old",
                    "sha256": "a" * 64,
                    "dest_path": str(missing),
                    "install_dir": str(root),
                    "newly_created": True,
                    "faces": [],
                },
                {
                    "schema": winfonts.SCHEMA,
                    "record": "font_file",
                    "transaction_id": "new",
                    "sha256": "b" * 64,
                    "dest_path": str(missing),
                    "install_dir": str(root),
                    "newly_created": True,
                    "faces": [],
                },
            ]
            manifest.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                manifest=str(manifest),
                dry_run=False,
                drop_missing=True,
                drop_modified=False,
                drop_symlink=False,
                drop_malformed=False,
                compact=True,
            )

            with redirect_stdout(io.StringIO()):
                code = winfonts.repair_command(args)

            self.assertEqual(code, winfonts.EXIT_OK)
            self.assertFalse(manifest.exists())

    def test_repair_compact_keeps_the_duplicate_record_that_still_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            font = root / "font.ttf"
            font.write_bytes(b"current")
            manifest = root / "manifest.jsonl"
            bad = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": "0" * 64,
                "dest_path": str(font),
                "install_dir": str(root),
                "newly_created": True,
                "faces": [],
            }
            good = {
                **bad,
                "sha256": winfonts.sha256_file(font),
            }
            manifest.write_text(
                json.dumps(bad) + "\n" + json.dumps(good) + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                manifest=str(manifest),
                dry_run=False,
                drop_missing=False,
                drop_modified=False,
                drop_symlink=False,
                drop_malformed=False,
                compact=True,
                recover_pending=False,
            )

            with redirect_stdout(io.StringIO()):
                code = winfonts.repair_command(args)
            records = winfonts.read_manifest(manifest)

        self.assertEqual(code, winfonts.EXIT_OK)
        fonts = [record for record in records if record.get("record") == "font_file"]
        self.assertEqual(len(fonts), 1)
        self.assertEqual(fonts[0]["sha256"], good["sha256"])

    def test_repair_dry_run_does_not_change_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest.jsonl"
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": "a" * 64,
                "dest_path": str(root / "missing.ttf"),
                "install_dir": str(root),
                "newly_created": True,
                "faces": [],
            }
            original = json.dumps(record) + "\n"
            manifest.write_text(original, encoding="utf-8")
            args = argparse.Namespace(
                manifest=str(manifest),
                dry_run=True,
                drop_missing=False,
                drop_modified=False,
                drop_symlink=False,
                drop_malformed=False,
                compact=False,
            )

            with redirect_stdout(io.StringIO()):
                code = winfonts.repair_command(args)

            self.assertEqual(code, winfonts.EXIT_OK)
            self.assertEqual(manifest.read_text(encoding="utf-8"), original)

    def test_repair_recovers_hash_matching_pending_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest.jsonl"
            journal = winfonts.pending_journal_path(manifest)
            install_dir = root / "fonts"
            install_dir.mkdir()
            pending = install_dir / "pending.ttf"
            pending.write_bytes(b"pending")
            journal_record = {
                "schema": winfonts.SCHEMA,
                "record": "pending_install",
                "transaction_id": "pending",
                "planned_files": [
                    {
                        "dest_path": str(pending),
                        "install_dir": str(install_dir),
                        "sha256": winfonts.sha256_file(pending),
                    }
                ],
            }
            journal.write_text(json.dumps(journal_record) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                manifest=str(manifest),
                dry_run=False,
                drop_missing=False,
                drop_modified=False,
                drop_symlink=False,
                drop_malformed=False,
                compact=False,
                recover_pending=True,
            )

            with redirect_stdout(io.StringIO()):
                code = winfonts.repair_command(args)

            self.assertEqual(code, winfonts.EXIT_OK)
            self.assertFalse(pending.exists())
            self.assertFalse(journal.exists())

    def test_repair_preserves_pending_file_already_committed_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest.jsonl"
            journal = winfonts.pending_journal_path(manifest)
            install_dir = root / "fonts"
            install_dir.mkdir()
            committed = install_dir / "committed.ttf"
            committed.write_bytes(b"committed")
            digest = winfonts.sha256_file(committed)
            manifest_record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "dest_path": str(committed),
                "install_dir": str(install_dir),
                "sha256": digest,
                "newly_created": True,
                "faces": [],
            }
            journal_record = {
                "schema": winfonts.SCHEMA,
                "record": "pending_install",
                "transaction_id": "pending",
                "planned_files": [
                    {
                        "dest_path": str(committed),
                        "install_dir": str(install_dir),
                        "sha256": digest,
                    }
                ],
            }
            manifest.write_text(json.dumps(manifest_record) + "\n", encoding="utf-8")
            journal.write_text(json.dumps(journal_record) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                manifest=str(manifest),
                dry_run=False,
                drop_missing=False,
                drop_modified=False,
                drop_symlink=False,
                drop_malformed=False,
                compact=False,
                recover_pending=True,
            )

            with redirect_stdout(io.StringIO()):
                code = winfonts.repair_command(args)

            self.assertEqual(code, winfonts.EXIT_OK)
            self.assertEqual(committed.read_bytes(), b"committed")
            self.assertFalse(journal.exists())

    def test_status_reports_a_pending_journal_without_a_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "manifest.jsonl"
            journal = winfonts.pending_journal_path(manifest)
            journal.write_text(
                json.dumps(
                    {
                        "schema": winfonts.SCHEMA,
                        "record": "pending_install",
                        "planned_files": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(manifest=str(manifest), json=False)
            output = io.StringIO()

            with redirect_stdout(output):
                code = winfonts.status_command(args)

        self.assertEqual(code, winfonts.EXIT_PARTIAL)
        self.assertIn("Pending installation journal", output.getvalue())

    def test_manifest_failure_rolls_back_and_never_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            dest = root / "dest"
            tmp = root / "tmp"
            tmp.mkdir()
            manifest = root / "manifest.jsonl"
            plan = winfonts.SourcePlan(
                source=source,
                info=winfonts.SourceInfo("loose-font-directory", root=source),
                dest=dest,
            )
            item = candidate(source, dest)
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "dest_path": str(dest / "font.ttf"),
                "sha256": item.sha256,
            }

            def decide(
                candidates: list[winfonts.Candidate],
                _dest: Path,
                _policy: str,
            ) -> dict[str, int]:
                candidates[0].state = "install"
                candidates[0].reason = "new-file"
                candidates[0].target = dest / "font.ttf"
                return {"new-file": 1}

            output = io.StringIO()
            with (
                patch.object(winfonts, "extract_candidates", return_value=([item], [])),
                patch.object(winfonts, "build_candidate_metadata"),
                patch.object(winfonts, "decide_candidates", side_effect=decide),
                patch.object(winfonts, "install_candidate", return_value=record),
                patch.object(
                    winfonts,
                    "write_manifest_atomic",
                    side_effect=[None, OSError("primary manifest failure"), None],
                ) as writer,
                patch.object(winfonts, "rollback_files", return_value=[]) as rollback,
                patch.object(winfonts, "run"),
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(winfonts.WinfontsError) as raised:
                    winfonts._install_with_temp(
                        [plan],
                        tmp,
                        manifest,
                        [],
                        install_args(),
                    )

            self.assertEqual(raised.exception.code, winfonts.EXIT_IO)
            self.assertIn("installation rolled back", str(raised.exception))
            self.assertEqual(writer.call_count, 3)
            self.assertEqual(rollback.call_count, 1)
            aborted_records = writer.call_args_list[2].args[1]
            self.assertEqual(aborted_records[-1]["state"], "aborted")
            self.assertNotIn("Installed:", output.getvalue())

    def test_empty_install_does_not_write_a_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            dest = root / "dest"
            tmp = root / "tmp"
            tmp.mkdir()
            manifest = root / "manifest.jsonl"
            plan = winfonts.SourcePlan(
                source=source,
                info=winfonts.SourceInfo("loose-font-directory", root=source),
                dest=dest,
            )
            item = candidate(source, dest)
            item.state = "skip"
            item.reason = "identical-file"

            with (
                patch.object(winfonts, "extract_candidates", return_value=([item], [])),
                patch.object(winfonts, "build_candidate_metadata"),
                patch.object(
                    winfonts,
                    "decide_candidates",
                    return_value={"identical-file": 1},
                ),
                patch.object(winfonts, "write_manifest_atomic") as writer,
                patch.object(winfonts, "run") as cache,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                code = winfonts._install_with_temp(
                    [plan],
                    tmp,
                    manifest,
                    [],
                    install_args(duplicate_policy="skip-existing"),
                )

            self.assertEqual(code, winfonts.EXIT_DUPLICATES)
            writer.assert_not_called()
            cache.assert_not_called()

    def test_corrupt_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "manifest.jsonl"
            manifest.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaises(winfonts.WinfontsError) as raised:
                winfonts.read_manifest(manifest)
        self.assertEqual(raised.exception.code, winfonts.EXIT_USAGE)

    def test_rollback_reports_files_it_cannot_safely_remove(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "font.ttf"
            path.write_bytes(b"modified")
            failures = winfonts.rollback_files(
                [
                    {
                        "dest_path": str(path),
                        "sha256": "0" * 64,
                    }
                ]
            )
        self.assertEqual(len(failures), 1)
        self.assertIn("content changed", failures[0])

    def test_uninstall_does_not_follow_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_dir = root / "fonts"
            install_dir.mkdir()
            outside = root / "outside.ttf"
            outside.write_bytes(b"protected")
            link = install_dir / "managed.ttf"
            link.symlink_to(outside)
            manifest = root / "manifest.jsonl"
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": winfonts.sha256_file(outside),
                "dest_path": str(link),
                "install_dir": str(install_dir),
                "newly_created": True,
                "faces": [],
            }
            manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
            args = argparse.Namespace(manifest=str(manifest), dry_run=False)

            with patch.object(winfonts, "run"), redirect_stdout(io.StringIO()):
                code = winfonts.uninstall_command(args)

            self.assertEqual(code, winfonts.EXIT_PARTIAL)
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_bytes(), b"protected")

    def test_uninstall_rejects_a_path_outside_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_dir = root / "fonts"
            install_dir.mkdir()
            outside = root / "outside.ttf"
            outside.write_bytes(b"protected")
            manifest = root / "manifest.jsonl"
            record = {
                "schema": winfonts.SCHEMA,
                "record": "font_file",
                "sha256": winfonts.sha256_file(outside),
                "dest_path": str(outside),
                "install_dir": str(install_dir),
                "newly_created": True,
                "faces": [],
            }
            manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
            args = argparse.Namespace(manifest=str(manifest), dry_run=False)

            with patch.object(winfonts, "run"), redirect_stdout(io.StringIO()):
                code = winfonts.uninstall_command(args)

            self.assertEqual(code, winfonts.EXIT_PARTIAL)
            self.assertEqual(outside.read_bytes(), b"protected")

    def test_uninstall_dry_run_counts_only_removable_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_dir = root / "fonts"
            install_dir.mkdir()
            removable = install_dir / "removable.ttf"
            protected = install_dir / "protected.ttf"
            removable.write_bytes(b"one")
            protected.write_bytes(b"two")
            records = [
                {
                    "record": "font_file",
                    "dest_path": str(removable),
                    "install_dir": str(install_dir),
                    "newly_created": True,
                    "_status": "ok",
                },
                {
                    "record": "font_file",
                    "dest_path": str(protected),
                    "install_dir": str(install_dir),
                    "newly_created": False,
                    "_status": "ok",
                },
            ]
            args = argparse.Namespace(dry_run=True)
            output = io.StringIO()

            with redirect_stdout(output):
                code = winfonts._uninstall_locked(
                    args,
                    root / "manifest.jsonl",
                    records,
                    {},
                )

        self.assertEqual(code, winfonts.EXIT_OK)
        self.assertIn("Would remove: 1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
