import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import office_font_carver as carver_module
import winfonts_engine as winfonts


class OfficeCarverTests(unittest.TestCase):
    def test_keep_duplicates_writes_distinct_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            carver = carver_module.Carver(
                output=output,
                dry_run=False,
                max_font_size=1024,
                source_path="/media/Office.img",
                keep_duplicates=True,
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                carver._save(b"same font bytes", ".ttf", 1)
                carver._save(b"same font bytes", ".ttf", 2)

            files = sorted(output.glob("*.ttf"))

        self.assertEqual(carver.extracted, 2)
        self.assertEqual(len(files), 2)
        self.assertNotEqual(files[0].name, files[1].name)

    def test_office_records_can_preserve_duplicate_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first.ttf"
            second = root / "second.ttf"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            records = root / "records.jsonl"
            digest = winfonts.sha256_file(first)
            records.write_text(
                "\n".join(
                    [
                        (
                            '{"record":"candidate","sha256":"%s",'
                            '"output_path":"%s","filename":"first.ttf"}'
                        )
                        % (digest, first),
                        (
                            '{"record":"candidate","sha256":"%s",'
                            '"output_path":"%s","filename":"second.ttf"}'
                        )
                        % (digest, second),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            candidates = winfonts.candidates_from_office_records(
                records,
                keep_duplicate_content=True,
            )

        self.assertEqual(len(candidates), 2)


if __name__ == "__main__":
    unittest.main()
