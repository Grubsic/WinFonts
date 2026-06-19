import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import winfonts_engine as winfonts


def make_face(
    family: str = "Aptos",
    style: str = "Bold",
    fullname: str = "Aptos Bold",
    postscript: str = "Aptos-Bold",
) -> winfonts.Face:
    return winfonts.Face(
        index="0",
        family=family,
        style=style,
        fullname=fullname,
        postscript=postscript,
        revision=1,
        fontformat="TrueType",
        spacing="",
        color="False",
        variable="False",
    )


def make_candidate(
    filename: str,
    digest: str = "a" * 64,
    faces: list[winfonts.Face] | None = None,
) -> winfonts.Candidate:
    return winfonts.Candidate(
        path=Path(filename),
        original_filename=filename,
        source_type="test",
        source_path="test",
        sha256=digest,
        faces=faces or [make_face()],
    )


class FontNamingTests(unittest.TestCase):
    def test_opaque_source_name_uses_font_metadata(self) -> None:
        candidate = make_candidate("ba67safs67d6asd6732h23f7uhn2809vgh29.ttf")
        self.assertEqual(winfonts.preferred_installed_filename(candidate), "Aptos-Bold.ttf")

    def test_readable_source_name_is_preserved(self) -> None:
        candidate = make_candidate("arialbd.TTF")
        self.assertEqual(winfonts.preferred_installed_filename(candidate), "arialbd.ttf")

    def test_unicode_metadata_is_preserved(self) -> None:
        candidate = make_candidate(
            "0123456789abcdef0123456789abcdef.ttc",
            faces=[make_face("游ゴシック", "Regular", "游ゴシック", "")],
        )
        self.assertEqual(winfonts.preferred_installed_filename(candidate), "游ゴシック.ttc")

    def test_collection_uses_family_name(self) -> None:
        face = make_face()
        candidate = make_candidate(
            "0123456789abcdef0123456789abcdef.ttc",
            faces=[face, face],
        )
        self.assertEqual(winfonts.preferred_installed_filename(candidate), "Aptos-Collection.ttc")

    def test_hash_is_only_added_for_a_real_collision(self) -> None:
        candidate = make_candidate("ba67safs67d6asd6732h23f7uhn2809vgh29.ttf")
        with tempfile.TemporaryDirectory() as raw_dest:
            dest = Path(raw_dest)
            target, reason = winfonts.choose_target(dest, candidate)
            self.assertEqual(target.name, "Aptos-Bold.ttf")
            self.assertEqual(reason, "new-file")

            target.write_bytes(b"different font")
            collision, reason = winfonts.choose_target(dest, candidate)
            self.assertEqual(collision.name, "Aptos-Bold-aaaaaaaaaaaa.ttf")
            self.assertEqual(reason, "filename-collision")

    def test_planned_targets_cannot_overwrite_each_other(self) -> None:
        candidate = make_candidate("ba67safs67d6asd6732h23f7uhn2809vgh29.ttf")
        with tempfile.TemporaryDirectory() as raw_dest:
            dest = Path(raw_dest)
            reserved = {dest / "Aptos-Bold.ttf": "b" * 64}
            target, reason = winfonts.choose_target(dest, candidate, reserved)
            self.assertEqual(target.name, "Aptos-Bold-aaaaaaaaaaaa.ttf")
            self.assertEqual(reason, "filename-collision")

    def test_keep_all_never_reuses_an_identical_existing_file(self) -> None:
        candidate = make_candidate("ba67safs67d6asd6732h23f7uhn2809vgh29.ttf")
        with tempfile.TemporaryDirectory() as raw_dest:
            dest = Path(raw_dest)
            original = dest / "Aptos-Bold.ttf"
            original.write_bytes(b"same font")
            candidate.sha256 = winfonts.sha256_file(original)

            target, reason = winfonts.choose_target(dest, candidate, reuse_identical=False)

            self.assertNotEqual(target, original)
            self.assertEqual(reason, "filename-collision")
            self.assertFalse(target.exists())

    def test_prefer_newer_skips_an_older_candidate(self) -> None:
        older = make_face()
        older.revision = 1
        newer = make_face()
        newer.revision = 2
        candidate = make_candidate("aptos-bold.ttf", faces=[older])
        with tempfile.TemporaryDirectory() as raw_dest:
            dest = Path(raw_dest)
            with patch(
                "winfonts_engine.installed_font_index",
                return_value=({}, {older.face_key(): [newer]}),
            ):
                winfonts.decide_candidates([candidate], dest, "prefer-newer")

        self.assertEqual(candidate.state, "skip")
        self.assertEqual(candidate.reason, "older-version")


if __name__ == "__main__":
    unittest.main()
