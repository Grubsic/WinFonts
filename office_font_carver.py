#!/usr/bin/env python3
"""Extract candidate desktop fonts from Office Click-to-Run payloads.

This is intentionally a helper for winfonts, not a standalone installer.  It
uses only the Python standard library: Office stream.*.dat files are scanned for
zlib members, inflated one member at a time, then valid SFNT fonts are carved
from the decompressed data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

READ_SIZE = 1024 * 1024
MAX_CHUNK = 1024 * 1024
DEFAULT_MAX_FONT = 128 * 1024 * 1024
PROGRESS_SECONDS = 2.0
PROGRESS_BYTES = 128 * 1024 * 1024
CANDIDATE_LOG_LIMIT = 40
CANDIDATE_LOG_EVERY = 25
SIGNATURE_RE = re.compile(b"\x00\x01\x00\x00|OTTO|true|typ1|ttcf")


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def human_size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


class Progress:
    def __init__(self, stream_name: str, total_bytes: int) -> None:
        self.stream_name = stream_name
        self.total_bytes = total_bytes
        self.started = time.monotonic()
        self.last_report = 0.0
        self.last_compressed = 0
        self.compressed = 0
        self.members = 0
        self.inflated = 0
        self.candidates = 0
        self.duplicates = 0
        self.buffer_size = 0

    def update(
        self,
        *,
        compressed: int | None = None,
        members: int | None = None,
        inflated: int | None = None,
        candidates: int | None = None,
        duplicates: int | None = None,
        buffer_size: int | None = None,
    ) -> None:
        if compressed is not None:
            self.compressed = compressed
        if members is not None:
            self.members = members
        if inflated is not None:
            self.inflated = inflated
        if candidates is not None:
            self.candidates = candidates
        if duplicates is not None:
            self.duplicates = duplicates
        if buffer_size is not None:
            self.buffer_size = buffer_size

    def report(self, phase: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force:
            by_time = now - self.last_report >= PROGRESS_SECONDS
            by_bytes = self.compressed - self.last_compressed >= PROGRESS_BYTES
            if not by_time and not by_bytes:
                return

        elapsed = max(0.001, now - self.started)
        pct = 0.0 if self.total_bytes <= 0 else min(100.0, self.compressed * 100.0 / self.total_bytes)
        rate = self.compressed / elapsed
        eprint(
            "office progress: "
            f"{phase}; "
            f"read {human_size(self.compressed)}/{human_size(self.total_bytes)} ({pct:.1f}%); "
            f"rate {human_size(int(rate))}/s; "
            f"members={self.members}; "
            f"inflated={human_size(self.inflated)}; "
            f"candidates={self.candidates}; "
            f"duplicates={self.duplicates}; "
            f"scan_buffer={human_size(self.buffer_size)}; "
            f"elapsed={elapsed:.0f}s"
        )
        self.last_report = now
        self.last_compressed = self.compressed


def be16(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def be32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def align4(value: int) -> int:
    return (value + 3) & ~3


def table_tag(tag: bytes) -> bool:
    return len(tag) == 4 and all(0x20 <= byte <= 0x7E for byte in tag)


@dataclass(frozen=True)
class Probe:
    state: str
    length: int = 0
    extension: str = ""
    required: int = 0


def probe_sfnt(
    data: bytes | bytearray,
    directory_offset: int,
    table_origin: int,
    max_end: int,
) -> tuple[str, int, int]:
    if len(data) < directory_offset + 12:
        return "need_more", 0, directory_offset + 12

    scaler = bytes(data[directory_offset : directory_offset + 4])
    if scaler not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        return "invalid", 0, 0

    count = be16(data, directory_offset + 4)
    if not 1 <= count <= 256:
        return "invalid", 0, 0

    directory_end = directory_offset + 12 + count * 16
    if directory_end > max_end:
        return "invalid", 0, 0
    if len(data) < directory_end:
        return "need_more", 0, directory_end

    tags: set[bytes] = set()
    extent = directory_end
    for index in range(count):
        record = directory_offset + 12 + index * 16
        tag = bytes(data[record : record + 4])
        if not table_tag(tag) or tag in tags:
            return "invalid", 0, 0
        tags.add(tag)

        offset = table_origin + be32(data, record + 8)
        length = be32(data, record + 12)
        end = offset + length
        if end < offset or end > max_end:
            return "invalid", 0, 0
        extent = max(extent, end)

    if b"name" not in tags or not (tags & {b"head", b"bhed", b"CFF ", b"CFF2"}):
        return "invalid", 0, 0

    return "valid", extent, 0


def probe_font(data: bytes | bytearray, start: int, max_font_size: int) -> Probe:
    available = len(data) - start
    if available < 4:
        return Probe("need_more", required=start + 4)

    signature = bytes(data[start : start + 4])
    max_end = start + max_font_size

    if signature in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        state, extent, required = probe_sfnt(data, start, start, max_end)
        if state == "need_more":
            return Probe("need_more", required=required)
        if state == "invalid":
            return Probe("invalid")
        length = align4(extent - start)
        if not 64 <= length <= max_font_size:
            return Probe("invalid")
        return Probe("valid", length=length, extension=".otf" if signature == b"OTTO" else ".ttf")

    if signature == b"ttcf":
        if available < 12:
            return Probe("need_more", required=start + 12)
        version = be32(data, start + 4)
        count = be32(data, start + 8)
        if version not in (0x00010000, 0x00020000) or not 1 <= count <= 64:
            return Probe("invalid")

        header_end = start + 12 + count * 4 + (12 if version == 0x00020000 else 0)
        if header_end > max_end:
            return Probe("invalid")
        if len(data) < header_end:
            return Probe("need_more", required=header_end)

        extent = header_end
        for index in range(count):
            relative = be32(data, start + 12 + index * 4)
            if relative >= max_font_size:
                return Probe("invalid")
            state, sub_extent, required = probe_sfnt(data, start + relative, start, max_end)
            if state == "need_more":
                return Probe("need_more", required=required)
            if state == "invalid":
                return Probe("invalid")
            extent = max(extent, sub_extent)

        if version == 0x00020000:
            dsig = start + 12 + count * 4
            dsig_length = be32(data, dsig + 4)
            dsig_offset = be32(data, dsig + 8)
            if dsig_length:
                if dsig_offset + dsig_length > max_font_size:
                    return Probe("invalid")
                extent = max(extent, start + dsig_offset + dsig_length)

        length = align4(extent - start)
        if not 64 <= length <= max_font_size:
            return Probe("invalid")
        return Probe("valid", length=length, extension=".ttc")

    return Probe("invalid")


def sfnt_table(data: bytes, directory_offset: int, table_origin: int, wanted: bytes) -> bytes | None:
    if len(data) < directory_offset + 12:
        return None
    count = be16(data, directory_offset + 4)
    directory_end = directory_offset + 12 + count * 16
    if not 1 <= count <= 256 or directory_end > len(data):
        return None
    for index in range(count):
        record = directory_offset + 12 + index * 16
        if data[record : record + 4] != wanted:
            continue
        offset = table_origin + be32(data, record + 8)
        length = be32(data, record + 12)
        if offset + length > len(data):
            return None
        return data[offset : offset + length]
    return None


def decode_name(platform: int, raw: bytes) -> str | None:
    try:
        if platform in (0, 3):
            return raw.decode("utf-16-be", errors="strict")
        if platform == 1:
            return raw.decode("mac_roman", errors="strict")
        return raw.decode("latin-1", errors="strict")
    except UnicodeDecodeError:
        return None


def font_name(blob: bytes, extension: str) -> str | None:
    if extension == ".ttc":
        if len(blob) < 16:
            return None
        directory_offset = be32(blob, 12)
    elif extension in (".ttf", ".otf"):
        directory_offset = 0
    else:
        return None

    table = sfnt_table(blob, directory_offset, 0, b"name")
    if not table or len(table) < 6:
        return None

    count = be16(table, 2)
    strings = be16(table, 4)
    if count > 4096 or 6 + count * 12 > len(table) or strings > len(table):
        return None

    candidates: list[tuple[int, int, str]] = []
    for index in range(count):
        record = 6 + index * 12
        platform = be16(table, record)
        language = be16(table, record + 4)
        name_id = be16(table, record + 6)
        length = be16(table, record + 8)
        offset = be16(table, record + 10)
        begin = strings + offset
        end = begin + length
        if end > len(table) or name_id not in (1, 2, 4, 6):
            continue
        text = decode_name(platform, table[begin:end])
        if not text:
            continue
        text = " ".join(text.replace("\x00", "").split()).strip()
        if not text:
            continue
        name_priority = {4: 0, 6: 1, 1: 2, 2: 3}[name_id]
        platform_priority = 0 if platform == 3 else 1 if platform == 0 else 2
        language_priority = 0 if language in (0, 0x0409) else 1
        candidates.append((name_priority * 10 + platform_priority * 2 + language_priority, name_id, text))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    full = next((text for _, name_id, text in candidates if name_id == 4), None)
    if full:
        return full
    postscript = next((text for _, name_id, text in candidates if name_id == 6), None)
    if postscript:
        return postscript
    family = next((text for _, name_id, text in candidates if name_id == 1), None)
    style = next((text for _, name_id, text in candidates if name_id == 2), None)
    if family and style and style.lower() not in ("regular", "normal"):
        return f"{family} {style}"
    return family or style


def safe_name(value: str) -> str:
    value = value.strip().replace(os.sep, "-")
    if os.altsep:
        value = value.replace(os.altsep, "-")
    value = re.sub(r"[^A-Za-z0-9._+() -]+", "-", value)
    value = re.sub(r"[ .-]+", "-", value).strip("-._")
    return value[:120] or "font"


class Carver:
    def __init__(
        self,
        output: Path,
        dry_run: bool,
        max_font_size: int,
        records: TextIO | None = None,
    ) -> None:
        self.output = output
        self.dry_run = dry_run
        self.max_font_size = max_font_size
        self.records = records
        self.buffer = bytearray()
        self.base = 0
        self.scan = 0
        self.hashes: set[str] = set()
        self.extracted = 0
        self.duplicates = 0
        self.progress: Progress | None = None
        self.current_stream = ""

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self.buffer.extend(chunk)
            self._scan(final=False)
            self._report_progress("scanning inflated data")

    def finish(self) -> None:
        self._scan(final=True)
        self.buffer.clear()
        self._report_progress("finalizing stream", force=True)

    def _report_progress(self, phase: str, *, force: bool = False) -> None:
        if self.progress is None:
            return
        self.progress.update(
            candidates=self.extracted,
            duplicates=self.duplicates,
            buffer_size=len(self.buffer),
        )
        self.progress.report(phase, force=force)

    def _next_signature(self, start: int) -> int:
        match = SIGNATURE_RE.search(self.buffer, start)
        return match.start() if match else -1

    def _discard(self, count: int) -> None:
        if count <= 0:
            return
        del self.buffer[:count]
        self.base += count
        self.scan = max(0, self.scan - count)

    def _scan(self, final: bool) -> None:
        while True:
            position = self._next_signature(self.scan)
            if position < 0:
                self._discard(len(self.buffer) if final else max(0, len(self.buffer) - 3))
                return

            probe = probe_font(self.buffer, position, self.max_font_size)
            if probe.state == "invalid":
                self.scan = position + 1
                continue
            if probe.state == "need_more":
                if final:
                    self.scan = position + 1
                    continue
                self._discard(position)
                self.scan = 0
                return

            end = position + probe.length
            if end > len(self.buffer):
                if final:
                    self.scan = position + 1
                    continue
                self._discard(position)
                self.scan = 0
                return

            self._save(bytes(self.buffer[position:end]), probe.extension, self.base + position)
            self._discard(end)
            self.scan = 0

    def _save(self, blob: bytes, extension: str, offset: int) -> None:
        digest = hashlib.sha256(blob).hexdigest()
        if digest in self.hashes:
            self.duplicates += 1
            return
        self.hashes.add(digest)

        name = safe_name(font_name(blob, extension) or f"font-{offset:012x}")
        target = self.output / f"{name}{extension}"
        suffix = 2
        while target.exists():
            try:
                if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    self.duplicates += 1
                    return
            except OSError:
                pass
            target = self.output / f"{name}-{suffix}{extension}"
            suffix += 1

        if not self.dry_run:
            target.write_bytes(blob)
        self.extracted += 1
        if self.extracted <= CANDIDATE_LOG_LIMIT or self.extracted % CANDIDATE_LOG_EVERY == 0:
            eprint(f"office candidate {self.extracted}: {target.name} ({human_size(len(blob))})")
        elif self.extracted == CANDIDATE_LOG_LIMIT + 1:
            eprint(
                "office candidate log: "
                f"showing every {CANDIDATE_LOG_EVERY}th candidate after {CANDIDATE_LOG_LIMIT}"
            )
        print(f"{target.name}\t{len(blob)}\t{digest}")
        if self.records is not None:
            self.records.write(
                json.dumps(
                    {
                        "record": "candidate",
                        "source_type": "office-clicktorun",
                        "source_stream": self.current_stream,
                        "output_path": str(target),
                        "filename": target.name,
                        "size": len(blob),
                        "sha256": digest,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            self.records.flush()
        self._report_progress("found font candidate")


def zlib_header(data: bytes | bytearray, offset: int) -> bool:
    if offset + 2 > len(data):
        return False
    cmf = data[offset]
    flg = data[offset + 1]
    return (cmf & 0x0F) == 8 and (cmf >> 4) <= 7 and ((cmf << 8) + flg) % 31 == 0


def find_zlib(data: bytes | bytearray) -> int:
    for offset in range(max(0, len(data) - 1)):
        if zlib_header(data, offset):
            return offset
    return -1


def read_more(handle: BinaryIO, pending: bytearray) -> bool:
    chunk = handle.read(READ_SIZE)
    if not chunk:
        return False
    pending.extend(chunk)
    return True


def inflate_stream(path: Path, carver: Carver, progress: Progress) -> tuple[int, int]:
    members = 0
    inflated = 0
    compressed_offset = 0
    pending = bytearray()
    progress.report("starting stream", force=True)

    with path.open("rb") as handle:
        done = False
        while True:
            while len(pending) < 2 and not done:
                done = not read_more(handle, pending)
            if len(pending) < 2:
                break

            header = find_zlib(pending)
            while header < 0 and not done:
                if len(pending) > 1:
                    drop = len(pending) - 1
                    compressed_offset += drop
                    del pending[:drop]
                    progress.update(compressed=compressed_offset, members=members, inflated=inflated)
                    progress.report("searching zlib headers")
                done = not read_more(handle, pending)
                header = find_zlib(pending)
            if header < 0:
                break
            if header:
                compressed_offset += header
                del pending[:header]
                progress.update(compressed=compressed_offset, members=members, inflated=inflated)
                progress.report("skipping non-zlib data")

            member_start = compressed_offset
            decompressor = zlib.decompressobj()
            failed = False
            completed = False

            with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as member:
                member_size = 0
                while True:
                    if not pending:
                        done = not read_more(handle, pending)
                        if done and not decompressor.eof:
                            break
                    try:
                        out = decompressor.decompress(bytes(pending), MAX_CHUNK)
                    except zlib.error:
                        failed = True
                        break

                    consumed = len(pending) - len(decompressor.unconsumed_tail) - len(decompressor.unused_data)
                    compressed_offset += consumed
                    if decompressor.unconsumed_tail:
                        pending = bytearray(decompressor.unconsumed_tail)
                    elif decompressor.unused_data:
                        pending = bytearray(decompressor.unused_data)
                    else:
                        pending.clear()

                    if out:
                        member.write(out)
                        member_size += len(out)
                        progress.update(
                            compressed=compressed_offset,
                            members=members,
                            inflated=inflated + member_size,
                            candidates=carver.extracted,
                            duplicates=carver.duplicates,
                            buffer_size=len(carver.buffer),
                        )
                        progress.report("inflating zlib member")
                    if decompressor.eof:
                        completed = True
                        break
                    if not out and not pending and done:
                        break

                if completed:
                    member.seek(0)
                    while True:
                        chunk = member.read(MAX_CHUNK)
                        if not chunk:
                            break
                        progress.update(
                            compressed=compressed_offset,
                            members=members,
                            inflated=inflated + member_size,
                            candidates=carver.extracted,
                            duplicates=carver.duplicates,
                            buffer_size=len(carver.buffer),
                        )
                        progress.report("scanning decoded member")
                        carver.feed(chunk)
                    members += 1
                    inflated += member_size
                    progress.update(
                        compressed=compressed_offset,
                        members=members,
                        inflated=inflated,
                        candidates=carver.extracted,
                        duplicates=carver.duplicates,
                        buffer_size=len(carver.buffer),
                    )
                    progress.report("decoded zlib member")

            if failed:
                compressed_offset = member_start + 1
                handle.seek(compressed_offset)
                pending.clear()
                done = False
                progress.update(compressed=compressed_offset, members=members, inflated=inflated)
                progress.report("rejected zlib candidate")

    carver.finish()
    progress.update(
        compressed=max(compressed_offset, progress.compressed),
        members=members,
        inflated=inflated,
        candidates=carver.extracted,
        duplicates=carver.duplicates,
        buffer_size=len(carver.buffer),
    )
    progress.report("finished stream", force=True)
    return members, inflated


def stream_sort_key(path: Path) -> tuple[int, str]:
    meta = parse_stream_name(path)
    if meta["neutral"] and not meta["compatibility"]:
        rank = 0
    elif meta["neutral"]:
        rank = 1
    else:
        rank = 2
    return rank, str(path).lower()


def parse_stream_name(path: Path) -> dict[str, object]:
    name = path.name.lower()
    parts = name.split(".")
    meta: dict[str, object] = {
        "architecture": "",
        "language": "",
        "compatibility": "",
        "proof": False,
        "neutral": False,
    }
    if len(parts) >= 4 and parts[0] == "stream" and parts[-1] == "dat":
        meta["architecture"] = parts[1]
        meta["language"] = parts[2]
        extras = parts[3:-1]
        meta["proof"] = "proof" in extras
        meta["neutral"] = parts[2] == "x-none"
        for value in extras:
            if value in ("arm64x", "chpe"):
                meta["compatibility"] = value
                break
    return meta


def find_streams(
    root: Path,
    neutral_only: bool,
    architecture: str,
    languages: set[str],
    include_compat: bool,
) -> list[Path]:
    streams: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if not (name.startswith("stream.") and name.endswith(".dat")):
            continue
        if ".delta" in name or name.endswith(".man.dat"):
            continue
        meta = parse_stream_name(path)
        if architecture != "all" and meta["architecture"] != architecture:
            continue
        if neutral_only and not meta["neutral"]:
            continue
        if languages and meta["language"] not in languages:
            continue
        if meta["compatibility"] and not include_compat:
            continue
        streams.append(path)
    return sorted(streams, key=stream_sort_key)


def copy_loose_fonts(root: Path, output: Path, dry_run: bool, carver: Carver) -> int:
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".ttf", ".otf", ".ttc"):
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(blob).hexdigest()
        if digest in carver.hashes:
            carver.duplicates += 1
            continue
        carver.hashes.add(digest)

        name = safe_name(font_name(blob, path.suffix.lower()) or path.stem)
        target = output / f"{name}{path.suffix.lower()}"
        suffix = 2
        while target.exists():
            target = output / f"{name}-{suffix}{path.suffix.lower()}"
            suffix += 1
        if not dry_run:
            target.write_bytes(blob)
        count += 1
        carver.extracted += 1
        print(f"{target.name}\t{len(blob)}\t{digest}")
        if carver.records is not None:
            carver.records.write(
                json.dumps(
                    {
                        "record": "candidate",
                        "source_type": "loose-font-directory",
                        "source_stream": "",
                        "source_path": str(path),
                        "output_path": str(target),
                        "filename": target.name,
                        "size": len(blob),
                        "sha256": digest,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            carver.records.flush()
    return count


def parse_size(text: str) -> int:
    match = re.fullmatch(r"(?i)\s*(\d+)\s*([kmgt]?i?b)?\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("use bytes or a value such as 64MiB")
    value = int(match.group(1))
    unit = (match.group(2) or "b").lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
    }
    return value * factors[unit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="carve desktop fonts from Office Click-to-Run media")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--x-none-only", "--neutral-only", dest="neutral_only", action="store_true")
    parser.add_argument("--arch", choices=("x64", "x86", "all"), default="x64")
    parser.add_argument("--language", action="append", default=[])
    parser.add_argument("--include-compat", action="store_true")
    parser.add_argument("--records", type=Path)
    parser.add_argument("--max-font-size", type=parse_size, default=DEFAULT_MAX_FONT)
    args = parser.parse_args(argv)

    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        eprint(f"office carver: not a directory: {source}")
        return 2
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)

    records_handle: TextIO | None = None
    if args.records is not None:
        args.records.parent.mkdir(parents=True, exist_ok=True)
        records_handle = args.records.open("w", encoding="utf-8")

    carver = Carver(
        output=output,
        dry_run=args.dry_run,
        max_font_size=args.max_font_size,
        records=records_handle,
    )
    languages = {value.lower() for value in args.language}
    streams = find_streams(source, args.neutral_only, args.arch, languages, args.include_compat)
    all_streams = find_streams(source, False, "all", set(), True)
    total_stream_size = sum(path.stat().st_size for path in streams)
    all_stream_size = sum(path.stat().st_size for path in all_streams)

    eprint(f"office source: {source}")
    eprint(f"office output: {output}{' [dry run]' if args.dry_run else ''}")
    eprint(f"office mode: {'language-neutral streams only' if args.neutral_only else 'selected base streams'}")
    eprint(f"office architecture: {args.arch}; languages: {','.join(sorted(languages)) or 'all'}; compat: {'included' if args.include_compat else 'excluded'}")
    eprint(f"office streams selected: {len(streams)} / {len(all_streams)} ({human_size(total_stream_size)} / {human_size(all_stream_size)})")
    if not args.neutral_only and len(all_streams) > 1:
        eprint("office hint: add --office-x-none-only to scan fewer shared-resource streams first")

    try:
        loose = copy_loose_fonts(source, output, args.dry_run, carver)

        total_members = 0
        total_inflated = 0
        for index, stream in enumerate(streams, start=1):
            rel = stream.relative_to(source)
            stream_size = stream.stat().st_size
            eprint(f"office stream {index}/{len(streams)}: {rel} ({human_size(stream_size)})")
            progress = Progress(str(rel), stream_size)
            carver.progress = progress
            carver.current_stream = str(rel)
            members, inflated = inflate_stream(stream, carver, progress)
            total_members += members
            total_inflated += inflated
    except KeyboardInterrupt:
        eprint("office extraction interrupted")
        return 130
    finally:
        carver.progress = None
        if records_handle is not None:
            records_handle.close()

    eprint(f"office loose fonts: {loose}")
    eprint(f"office zlib members: {total_members}")
    eprint(f"office inflated: {human_size(total_inflated)}")
    eprint(f"office candidates: {carver.extracted}")
    eprint(f"office duplicates: {carver.duplicates}")

    if not streams and loose == 0:
        eprint("office carver: no Office stream.*.dat payloads or loose fonts found")
        return 3
    if carver.extracted == 0:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
