#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

APP = "winfonts"
VERSION = "0.4.0"
SCHEMA = 2
HASH_ALGO = "sha256"
FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc"}
MOUNT_IMAGE_EXTS = {".iso", ".img", ".udf", ".cdr", ".bin"}
MAX_CANDIDATE_SIZE = 256 * 1024 * 1024
FIELD_SEP = "\x1f"
REC_SEP = "\x1e"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_NO_FONTS = 4
EXIT_DUPLICATES = 5
EXIT_PARTIAL = 6
EXIT_VERIFY = 7
EXIT_LOCKED = 8
EXIT_IO = 10
EXIT_INTERRUPTED = 130

SCRIPT_DIR = Path(__file__).resolve().parent
OFFICE_CARVER = SCRIPT_DIR / "office_font_carver.py"
MICROSOFT_FONTS_DIR = "microsoft-fonts"


class WinfontsError(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UserContext:
    uid: int
    gid: int
    username: str
    home: Path
    from_sudo: bool


_TARGET_USER: UserContext | None = None


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def out(message: str) -> None:
    print(message, flush=True)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def target_user_context() -> UserContext:
    global _TARGET_USER
    if _TARGET_USER is not None:
        return _TARGET_USER

    sudo_uid = os.environ.get("SUDO_UID", "")
    sudo_gid = os.environ.get("SUDO_GID", "")
    if os.geteuid() == 0 and sudo_uid.isdigit() and int(sudo_uid) > 0:
        uid = int(sudo_uid)
        try:
            entry = pwd.getpwuid(uid)
        except KeyError as exc:
            raise WinfontsError(f"sudo user uid does not exist: {uid}", EXIT_USAGE) from exc
        gid = int(sudo_gid) if sudo_gid.isdigit() and int(sudo_gid) > 0 else entry.pw_gid
        username = os.environ.get("SUDO_USER") or entry.pw_name
        home = Path(entry.pw_dir).expanduser().resolve()
        _TARGET_USER = UserContext(uid=uid, gid=gid, username=username, home=home, from_sudo=True)
        return _TARGET_USER

    uid = os.geteuid()
    try:
        entry = pwd.getpwuid(uid)
        username = entry.pw_name
        gid = entry.pw_gid
        home = Path(os.environ.get("HOME") or entry.pw_dir).expanduser().resolve()
    except KeyError:
        username = os.environ.get("USER") or str(uid)
        gid = os.getegid()
        home_raw = os.environ.get("HOME", "")
        if not home_raw:
            raise WinfontsError("HOME is missing or empty", EXIT_USAGE)
        home = Path(home_raw).expanduser().resolve()
    _TARGET_USER = UserContext(uid=uid, gid=gid, username=username, home=home, from_sudo=False)
    return _TARGET_USER


def target_user_env() -> dict[str, str]:
    ctx = target_user_context()
    env = os.environ.copy()
    env["HOME"] = str(ctx.home)
    env["USER"] = ctx.username
    env["LOGNAME"] = ctx.username
    if ctx.from_sudo:
        env["XDG_DATA_HOME"] = str(ctx.home / ".local/share")
        env["XDG_CACHE_HOME"] = str(ctx.home / ".cache")
        env["XDG_CONFIG_HOME"] = str(ctx.home / ".config")
    return env


def target_user_preexec() -> Any:
    ctx = target_user_context()
    if os.geteuid() != 0 or not ctx.from_sudo:
        return None

    def demote() -> None:
        os.initgroups(ctx.username, ctx.gid)
        os.setgid(ctx.gid)
        os.setuid(ctx.uid)

    return demote


def path_under(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except ValueError:
        return False


def chown_target_if_sudo(path: Path) -> None:
    ctx = target_user_context()
    if not ctx.from_sudo or os.geteuid() != 0:
        return
    if not path_under(path, ctx.home):
        return
    try:
        os.chown(path, ctx.uid, ctx.gid, follow_symlinks=False)
    except FileNotFoundError:
        return


def chown_target_chain_if_sudo(path: Path) -> None:
    ctx = target_user_context()
    if not ctx.from_sudo or os.geteuid() != 0:
        return
    resolved = path.resolve(strict=False)
    try:
        rel = resolved.relative_to(ctx.home.resolve(strict=False))
    except ValueError:
        return
    current = ctx.home.resolve(strict=False)
    for part in rel.parts:
        current = current / part
        if current.exists() and not current.is_symlink():
            chown_target_if_sudo(current)


def run(
    argv: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    as_target_user: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = target_user_env() if as_target_user else None
    preexec_fn = target_user_preexec() if as_target_user else None
    try:
        return subprocess.run(
            argv,
            text=True,
            capture_output=capture,
            check=check,
            env=env,
            preexec_fn=preexec_fn,
        )
    except FileNotFoundError as exc:
        raise WinfontsError(f"missing dependency: {argv[0]}", EXIT_USAGE) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise WinfontsError(f"command failed: {' '.join(argv)}{suffix}") from exc


def reject_bad_path_text(value: str, label: str) -> None:
    if value == "":
        raise WinfontsError(f"{label} cannot be empty", EXIT_USAGE)
    if "\n" in value or "\t" in value or "\x00" in value:
        raise WinfontsError(f"{label} cannot contain tabs, newlines, or NUL bytes", EXIT_USAGE)


def home_dir() -> Path:
    ctx = target_user_context()
    if ctx.from_sudo:
        return ctx.home
    home = os.environ.get("HOME", "")
    if not home:
        raise WinfontsError("HOME is missing or empty", EXIT_USAGE)
    reject_bad_path_text(home, "HOME")
    return Path(home).expanduser().resolve()


def xdg_dir(env_name: str, fallback: Path) -> Path:
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return fallback
    reject_bad_path_text(raw, env_name)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise WinfontsError(f"{env_name} must be an absolute path", EXIT_USAGE)
    ctx = target_user_context()
    if ctx.from_sudo and not path_under(path, ctx.home):
        return fallback
    return path.resolve(strict=False)


def default_fonts_base() -> Path:
    base = xdg_dir("XDG_DATA_HOME", home_dir() / ".local/share")
    return base / "fonts" / MICROSOFT_FONTS_DIR


def default_state_dir() -> Path:
    return xdg_dir("XDG_STATE_HOME", home_dir() / ".local/state") / MICROSOFT_FONTS_DIR


def default_manifest() -> Path:
    return default_state_dir() / "manifest.jsonl"


def source_default_subdir(info: SourceInfo) -> str:
    if info.source_type.endswith("office-clicktorun"):
        return "office"
    if (
        info.source_type.endswith("windows-image")
        or info.source_type.endswith("windows-media")
        or info.source_type.endswith("windows-fonts-dir")
    ):
        return "windows"
    return "loose"


def default_dest_for_source(info: SourceInfo) -> Path:
    return default_fonts_base() / source_default_subdir(info)


def canonical_existing(path: Path, label: str) -> Path:
    reject_bad_path_text(str(path), label)
    try:
        return path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise WinfontsError(f"{label} not found: {path}", EXIT_NOT_FOUND) from exc


def canonical_for_create(path: Path, label: str, *, create_parent: bool = False) -> Path:
    reject_bad_path_text(str(path), label)
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    parent = expanded.parent
    if create_parent:
        try:
            parent.mkdir(parents=True, exist_ok=True)
            chown_target_chain_if_sudo(parent)
        except PermissionError as exc:
            raise WinfontsError(f"permission denied creating {label} parent: {parent}", EXIT_IO) from exc
        except OSError as exc:
            raise WinfontsError(f"could not create {label} parent {parent}: {exc}", EXIT_IO) from exc
    if not parent.exists():
        raise WinfontsError(f"{label} parent does not exist: {parent}", EXIT_NOT_FOUND)
    return parent.resolve(strict=True) / expanded.name.rstrip("/")


def canonical_future_path(path: Path, label: str) -> Path:
    reject_bad_path_text(str(path), label)
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.resolve(strict=False)


def safe_filename(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().replace(os.sep, "-")
    if os.altsep:
        value = value.replace(os.altsep, "-")
    value = re.sub(r"[^\w.+() -]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"[ .-]+", "-", value).strip("-._")
    if len(value.encode("utf-8")) > 120:
        shortened: list[str] = []
        byte_count = 0
        for char in value:
            char_size = len(char.encode("utf-8"))
            if byte_count + char_size > 120:
                break
            shortened.append(char)
            byte_count += char_size
        value = "".join(shortened).rstrip("-._ ")
    return value or "font"


def looks_opaque_font_stem(value: str) -> bool:
    stem = value.strip()
    lowered = stem.casefold()
    compact = "".join(char for char in stem if char.isalnum())
    ascii_compact = re.sub(r"[^A-Za-z0-9]", "", stem)
    if not compact:
        return True
    if re.fullmatch(r"font[-_ ]?[0-9a-f]{8,}", lowered):
        return True
    if re.fullmatch(r"[0-9a-f]{16,}", ascii_compact, flags=re.IGNORECASE):
        return True
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        lowered,
    ):
        return True
    # Office and Windows resource stores sometimes expose opaque alphanumeric
    # identifiers instead of filenames. Avoid treating long, digit-heavy IDs
    # as user-facing font names.
    if len(compact) >= 24 and compact.isalnum() and sum(char.isdigit() for char in compact) >= 4:
        return True
    return False


def first_fontconfig_name(value: str) -> str:
    # Fontconfig renders localized name lists separated by commas. The first
    # value is its preferred name for the current locale.
    return (value or "").split(",", 1)[0].strip()


def metadata_font_stem(cand: Candidate) -> str:
    if not cand.faces:
        return ""

    if len(cand.faces) > 1:
        families: list[str] = []
        seen: set[str] = set()
        for face in cand.faces:
            family = first_fontconfig_name(face.family)
            key = norm(family)
            if family and key not in seen and not looks_opaque_font_stem(family):
                seen.add(key)
                families.append(family)
        if families:
            return f"{families[0]} Collection"

    face = cand.faces[0]
    family = first_fontconfig_name(face.family)
    style = first_fontconfig_name(face.style)
    fullname = first_fontconfig_name(face.fullname)
    postscript = first_fontconfig_name(face.postscript)
    family_and_style = family
    if family and style and norm(style) not in {"regular", "normal", "roman"}:
        family_and_style = f"{family} {style}"

    for value in (fullname, postscript, family_and_style, family):
        if value and not looks_opaque_font_stem(value):
            return value
    return ""


def preferred_installed_filename(cand: Candidate) -> str:
    original = Path(cand.original_filename).name
    original_suffix = Path(original).suffix.casefold()
    path_suffix = cand.path.suffix.casefold()
    extension = original_suffix if original_suffix in FONT_EXTS else path_suffix
    if extension not in FONT_EXTS:
        extension = ".ttf"

    original_stem = Path(original).stem
    clean_original = safe_filename(original_stem)
    if clean_original == "font" or looks_opaque_font_stem(original_stem):
        clean_metadata = safe_filename(metadata_font_stem(cand))
        if clean_metadata != "font":
            return f"{clean_metadata}{extension}"
    return f"{clean_original}{extension}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def has_optical_image_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            for sector in range(16, 80):
                handle.seek(sector * 2048 + 1)
                if handle.read(5) in {b"CD001", b"NSR02", b"NSR03", b"BEA01", b"TEA01"}:
                    return True
    except OSError:
        return False
    return False


def should_try_mount_image(path: Path) -> bool:
    return path.suffix.casefold() in MOUNT_IMAGE_EXTS or has_optical_image_signature(path)


def mounted_source_prefix(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".iso" or has_optical_image_signature(path):
        return "iso-image"
    if suffix == ".img":
        return "img-image"
    return "disk-image"


def has_wim_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(8) == b"MSWIM\x00\x00\x00"
    except OSError:
        return False


@dataclass
class SourceInfo:
    source_type: str
    root: Path
    wim: Path | None = None
    windows_fonts: Path | None = None
    mounted: Path | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Face:
    index: str
    family: str
    style: str
    fullname: str
    postscript: str
    revision: int | None
    fontformat: str
    spacing: str
    color: str
    variable: str

    def exact_key(self) -> tuple[str, str, str, str, str]:
        return (
            norm(self.postscript),
            norm(self.family),
            norm(self.style),
            str(self.revision if self.revision is not None else ""),
            self.index,
        )

    def face_key(self) -> tuple[str, str, str]:
        return (norm(self.postscript), norm(self.family), norm(self.style))

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "family": self.family,
            "style": self.style,
            "fullname": self.fullname,
            "postscript": self.postscript,
            "revision": self.revision,
            "fontformat": self.fontformat,
            "spacing": self.spacing,
            "color": self.color,
            "variable": self.variable,
            "classification": classify_face(self),
        }


@dataclass
class Candidate:
    path: Path
    original_filename: str
    source_type: str
    source_path: str
    source_stream: str = ""
    wim_image_index: int | None = None
    size: int = 0
    sha256: str = ""
    faces: list[Face] = field(default_factory=list)
    state: str = "pending"
    reason: str = ""
    target: Path | None = None
    install_dir: Path | None = None


@dataclass
class SourcePlan:
    source: Path
    info: SourceInfo
    dest: Path


def norm(value: str) -> str:
    return " ".join((value or "").casefold().split())


def classify_face(face: Face) -> list[str]:
    tags: list[str] = []
    name = " ".join([face.family, face.fullname, face.postscript]).casefold()
    if "symbol" in name or "wingding" in name:
        tags.append("symbol")
    if any(token in name for token in ("codicon", "fabric mdl", "mdl2", "icon", "assets")):
        tags.append("icon")
    if any(token in name for token in ("segoe ui", "power", "outlook")):
        tags.append("office-ui")
    if face.spacing and face.spacing.strip() in {"90", "100", "110"}:
        tags.append("monospace")
    if face.variable.casefold() == "true":
        tags.append("variable")
    if face.color.casefold() == "true":
        tags.append("color")
    if "bitmap" in face.fontformat.casefold():
        tags.append("bitmap")
    if not tags:
        tags.append("document")
    return tags


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "Lock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            chown_target_chain_if_sudo(self.path.parent)
            self.handle = self.path.open("a+")
            chown_target_if_sudo(self.path)
        except OSError as exc:
            raise WinfontsError(f"could not create lock file {self.path}: {exc}", EXIT_IO) from exc
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WinfontsError("another winfonts install/uninstall/status operation is running", EXIT_LOCKED) from exc
        self.handle.write(f"{os.getpid()} {now_iso()}\n")
        self.handle.flush()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()


class TempManager:
    def __init__(self) -> None:
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.mounts: list[Path] = []

    def __enter__(self) -> Path:
        self.tempdir = tempfile.TemporaryDirectory(prefix="winfonts.")
        return Path(self.tempdir.name)

    def mount_image(self, source: Path, tmp: Path) -> Path:
        mount_dir = tmp / f"mount-{len(self.mounts) + 1}"
        mount_dir.mkdir()
        log(f"mounting disk image: {source}")
        proc = subprocess.run(
            ["mount", "-o", "loop,ro", "--", str(source), str(mount_dir)],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            try:
                mount_dir.rmdir()
            except OSError:
                pass
            details = (proc.stderr or proc.stdout or "").strip()
            if is_permission_mount_error(details):
                raise WinfontsError(mount_permission_message(source, details), EXIT_USAGE)
            if os.geteuid() != 0:
                raise WinfontsError(mount_nonroot_failure_message(source, details), EXIT_USAGE)
            suffix = f": {details}" if details else f" (exit code {proc.returncode})"
            raise WinfontsError(f"could not mount disk image {source}{suffix}", EXIT_IO)
        self.mounts.append(mount_dir)
        return mount_dir

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for mount_dir in reversed(self.mounts):
            try:
                run(["umount", "--", str(mount_dir)], capture=True, check=False)
            except Exception:
                pass
        if self.tempdir is not None:
            self.tempdir.cleanup()


def detect_source(source: Path, tmp: Path, tm: TempManager) -> SourceInfo:
    source = canonical_existing(source, "source")
    if source.is_symlink():
        raise WinfontsError(f"source cannot be a symlink: {source}", EXIT_USAGE)

    if source.is_file():
        if has_wim_signature(source):
            return SourceInfo("windows-image", root=source.parent, wim=source)
        if should_try_mount_image(source):
            mounted = tm.mount_image(source, tmp)
            info = detect_source(mounted, tmp, tm)
            info.source_type = mounted_source_prefix(source) + ":" + info.source_type
            info.mounted = mounted
            return info
        raise WinfontsError(
            f"unknown regular file type, not WIM/ESD or a mountable ISO/IMG-style disk image: {source}",
            EXIT_USAGE,
        )

    if not source.is_dir():
        raise WinfontsError(f"source must be a file or directory: {source}", EXIT_USAGE)

    types: list[SourceInfo] = []
    wims = known_wims(source)
    split_wims = list((source / "sources").glob("install*.swm")) if (source / "sources").exists() else []
    if split_wims:
        raise WinfontsError("split WIM files (install.swm) were detected; split WIM extraction is not supported yet", EXIT_USAGE)
    if len(wims) > 1:
        raise WinfontsError("multiple WIM/ESD files detected; pass the intended install.wim/install.esd directly", EXIT_USAGE)
    if len(wims) == 1:
        types.append(SourceInfo("windows-media", root=source, wim=wims[0]))
    elif (source / "Windows/Fonts").is_dir():
        types.append(SourceInfo("windows-fonts-dir", root=source, windows_fonts=source / "Windows/Fonts"))

    if is_office_clicktorun(source):
        types.append(SourceInfo("office-clicktorun", root=source))
    elif not types and has_loose_fonts(source):
        types.append(SourceInfo("loose-font-directory", root=source))

    if len(types) > 1:
        names = ", ".join(item.source_type for item in types)
        raise WinfontsError(f"ambiguous source contains multiple supported layouts: {names}", EXIT_USAGE)
    if not types:
        raise WinfontsError("source does not contain Windows fonts, Windows WIM/ESD media, Office Click-to-Run streams, or loose fonts", EXIT_NOT_FOUND)
    return types[0]


def known_wims(root: Path) -> list[Path]:
    preferred = [root / "sources/install.wim", root / "sources/install.esd"]
    found = [path for path in preferred if path.is_file()]
    if found:
        return found
    candidates = [path for path in root.rglob("*") if path.is_file() and path.name.casefold() in {"install.wim", "install.esd"}]
    return candidates


def is_office_clicktorun(root: Path) -> bool:
    data = root / "Office/Data"
    if not data.is_dir():
        return False
    for path in data.rglob("*"):
        if path.is_file():
            name = path.name.casefold()
            if name.startswith("stream.") and name.endswith(".dat"):
                return True
    return False


def has_loose_fonts(root: Path) -> bool:
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if path.is_file() and path.suffix.casefold() in FONT_EXTS:
            return True
    return False


def parse_fc_records(raw: str) -> list[Face]:
    faces: list[Face] = []
    for record in raw.split(REC_SEP):
        if not record.strip():
            continue
        fields = record.split(FIELD_SEP)
        while len(fields) < 10:
            fields.append("")
        revision: int | None
        try:
            revision = int(fields[5]) if fields[5].strip() else None
        except ValueError:
            revision = None
        faces.append(
            Face(
                index=fields[0].strip() or "0",
                family=fields[1].strip(),
                style=fields[2].strip(),
                fullname=fields[3].strip(),
                postscript=fields[4].strip(),
                revision=revision,
                fontformat=fields[6].strip(),
                spacing=fields[7].strip(),
                color=fields[8].strip(),
                variable=fields[9].strip(),
            )
        )
    return faces


def fc_scan(path: Path) -> list[Face]:
    fmt = FIELD_SEP.join(
        [
            "%{index}",
            "%{family}",
            "%{style}",
            "%{fullname}",
            "%{postscriptname}",
            "%{fontversion}",
            "%{fontformat}",
            "%{spacing}",
            "%{color}",
            "%{variable}",
        ]
    ) + REC_SEP
    proc = run(["fc-scan", "-f", fmt, "--", str(path)], capture=True, check=False)
    if proc.returncode != 0:
        return []
    return parse_fc_records(proc.stdout)


def installed_font_index() -> tuple[dict[tuple[str, str, str, str, str], list[dict[str, Any]]], dict[tuple[str, str, str], list[Face]]]:
    fmt = FIELD_SEP.join(
        [
            "%{file}",
            "%{index}",
            "%{family}",
            "%{style}",
            "%{fullname}",
            "%{postscriptname}",
            "%{fontversion}",
            "%{fontformat}",
            "%{spacing}",
            "%{color}",
            "%{variable}",
        ]
    ) + REC_SEP
    proc = run(["fc-list", "-f", fmt], capture=True, check=False, as_target_user=True)
    exact: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    by_face: dict[tuple[str, str, str], list[Face]] = {}
    for record in proc.stdout.split(REC_SEP):
        if not record.strip():
            continue
        fields = record.split(FIELD_SEP)
        while len(fields) < 11:
            fields.append("")
        face = parse_fc_records(FIELD_SEP.join(fields[1:]) + REC_SEP)
        if not face:
            continue
        item = face[0]
        exact.setdefault(item.exact_key(), []).append({"file": fields[0], "face": item.to_json()})
        by_face.setdefault(item.face_key(), []).append(item)
    return exact, by_face


def check_space(path: Path, required: int, label: str) -> None:
    usage = shutil.disk_usage(path)
    if usage.free < required:
        raise WinfontsError(f"insufficient free space for {label}: need {required} bytes, have {usage.free}", EXIT_IO)


def sudo_command_hint() -> str:
    executable = SCRIPT_DIR / "winfonts"
    return shlex.join(["sudo", str(executable), *sys.argv[1:]])


def is_permission_mount_error(details: str) -> bool:
    lowered = details.casefold()
    return any(
        token in lowered
        for token in (
            "permission denied",
            "operation not permitted",
            "must be superuser",
            "only root",
            "not authorized",
        )
    )


def mount_permission_message(source: Path, details: str) -> str:
    message = f"permission denied mounting disk image: {source}"
    if details:
        message += f"\nmount output: {details}"
    if os.geteuid() != 0:
        message += (
            "\nThis distro requires root privileges for loop mounts. "
            "Retry with sudo, or mount the image yourself and pass the mounted directory."
            f"\nExample: {sudo_command_hint()}"
        )
    else:
        message += "\nThe process is already root; check file permissions, loop-device access, and kernel filesystem support."
    return message


def mount_nonroot_failure_message(source: Path, details: str) -> str:
    message = f"could not mount disk image as a regular user: {source}"
    if details:
        message += f"\nmount output: {details}"
    message += (
        "\nIf this is a valid ISO/IMG/UDF-style image, retry with sudo, "
        "or mount it yourself and pass the mounted directory."
        f"\nExample: {sudo_command_hint()}"
    )
    return message


def extract_candidates(
    info: SourceInfo,
    tmp: Path,
    image: int | None,
    office_neutral_only: bool,
    office_arch: str,
    office_language: list[str],
) -> tuple[list[Candidate], list[str]]:
    notes: list[str] = []
    cand_dir = tmp / "candidates"
    cand_dir.mkdir()

    if info.source_type.endswith("windows-image") or info.source_type.endswith("windows-media"):
        assert info.wim is not None
        selected_image = image if image is not None else 1
        if image is None:
            log("Windows image index: 1 (auto; use --image N only if you want a different Windows edition)")
        else:
            log(f"Windows image index: {selected_image}")
        validate_wim_image(info.wim, selected_image)
        run(
            [
                "wimlib-imagex",
                "extract",
                str(info.wim),
                str(selected_image),
                "/Windows/Fonts/*",
                f"--dest-dir={cand_dir}",
                "--no-acls",
            ],
            capture=True,
        )
        return candidates_from_directory(cand_dir, info.source_type, str(info.wim), "", selected_image), notes

    if info.source_type.endswith("windows-fonts-dir"):
        assert info.windows_fonts is not None
        return candidates_from_directory(info.windows_fonts, "windows-fonts-dir", str(info.windows_fonts), "", None), notes

    if info.source_type.endswith("loose-font-directory"):
        return candidates_from_directory(info.root, "loose-font-directory", str(info.root), "", None), notes

    if info.source_type.endswith("office-clicktorun"):
        check_space(tmp, 512 * 1024 * 1024, "Office temporary extraction")
        records = tmp / "office-candidates.jsonl"
        argv = [
            sys.executable,
            str(OFFICE_CARVER),
            str(info.root),
            str(cand_dir),
            "--records",
            str(records),
            "--arch",
            office_arch,
        ]
        if office_neutral_only:
            argv.append("--neutral-only")
        for lang in office_language:
            argv.extend(["--language", lang])
        proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE)
        if proc.returncode == 130:
            raise WinfontsError("Office extraction interrupted", EXIT_INTERRUPTED)
        if proc.returncode == 3:
            raise WinfontsError("invalid Office Click-to-Run layout", EXIT_USAGE)
        if proc.returncode == 4:
            raise WinfontsError("Office extraction produced no font candidates", EXIT_NO_FONTS)
        if proc.returncode != 0:
            raise WinfontsError(f"Office extraction failed with exit code {proc.returncode}", proc.returncode)
        return candidates_from_office_records(records), notes

    raise WinfontsError(f"internal error: unsupported source type: {info.source_type}")


def candidates_from_directory(root: Path, source_type: str, source_path: str, stream: str, image: int | None) -> list[Candidate]:
    result: list[Candidate] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if path.is_file() and path.suffix.casefold() in FONT_EXTS:
            result.append(
                Candidate(
                    path=path.resolve(strict=False),
                    original_filename=path.name,
                    source_type=source_type,
                    source_path=source_path,
                    source_stream=stream,
                    wim_image_index=image,
                )
            )
    return result


def candidates_from_office_records(records: Path) -> list[Candidate]:
    result: list[Candidate] = []
    seen: set[str] = set()
    with records.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WinfontsError(f"malformed Office candidate record at line {line_no}: {exc}", EXIT_IO) from exc
            if record.get("record") != "candidate":
                continue
            digest = str(record.get("sha256", ""))
            if digest in seen:
                continue
            seen.add(digest)
            path = Path(str(record["output_path"]))
            result.append(
                Candidate(
                    path=path,
                    original_filename=str(record.get("filename", path.name)),
                    source_type="office-clicktorun",
                    source_path=str(record.get("source_path", "")),
                    source_stream=str(record.get("source_stream", "")),
                    size=int(record.get("size", 0) or 0),
                    sha256=digest,
                )
            )
    return result


def validate_wim_image(wim: Path, image: int) -> None:
    proc = run(["wimlib-imagex", "info", str(wim), str(image)], capture=True, check=False)
    if proc.returncode != 0:
        raise WinfontsError(f"WIM image index {image} is not available in {wim}", EXIT_USAGE)


def build_candidate_metadata(candidates: list[Candidate]) -> None:
    seen_hashes: dict[str, Candidate] = {}
    for cand in candidates:
        try:
            stat = cand.path.stat()
        except OSError as exc:
            cand.state = "invalid"
            cand.reason = f"unreadable:{exc.errno}"
            continue
        cand.size = cand.size or stat.st_size
        if cand.size == 0:
            cand.state = "invalid"
            cand.reason = "zero-byte"
            continue
        if cand.size > MAX_CANDIDATE_SIZE:
            cand.state = "unsupported"
            cand.reason = "too-large"
            continue
        if not cand.sha256:
            cand.sha256 = sha256_file(cand.path)
        if cand.sha256 in seen_hashes:
            cand.state = "skip"
            first = seen_hashes[cand.sha256]
            cand.reason = (
                "duplicate-content-in-source"
                if first.source_path == cand.source_path
                else "duplicate-content-across-sources"
            )
            continue
        seen_hashes[cand.sha256] = cand
        cand.faces = fc_scan(cand.path)
        if not cand.faces:
            cand.state = "malformed"
            cand.reason = "fc-scan-no-faces"


def target_content_state(target: Path, digest: str, reserved: dict[Path, str]) -> str:
    if target in reserved:
        return "identical" if reserved[target] == digest else "occupied"
    if target.is_symlink():
        return "symlink"
    if target.is_file():
        return "identical" if sha256_file(target) == digest else "occupied"
    if target.exists():
        return "occupied"
    return "free"


def choose_target(
    dest: Path,
    cand: Candidate,
    reserved: dict[Path, str] | None = None,
) -> tuple[Path, str]:
    reserved = reserved if reserved is not None else {}
    filename = preferred_installed_filename(cand)
    target = dest / filename
    state = target_content_state(target, cand.sha256, reserved)
    if state == "free":
        return target, "new-file"
    if state == "identical":
        return target, "identical-file"

    reason = "filename-collision-symlink" if state == "symlink" else "filename-collision"
    stem = Path(filename).stem
    extension = Path(filename).suffix
    digest_suffix = cand.sha256[:12]
    for index in range(1, 10000):
        suffix = digest_suffix if index == 1 else f"{digest_suffix}-{index}"
        candidate_target = dest / f"{stem}-{suffix}{extension}"
        candidate_state = target_content_state(candidate_target, cand.sha256, reserved)
        if candidate_state == "free":
            return candidate_target, reason
        if candidate_state == "identical":
            return candidate_target, "identical-file"
    raise WinfontsError(f"could not choose collision-free filename for {cand.original_filename}", EXIT_IO)


def candidate_dest(cand: Candidate, fallback_dest: Path) -> Path:
    return cand.install_dir if cand.install_dir is not None else fallback_dest


def print_destinations(destinations: list[Path]) -> None:
    if len(destinations) == 1:
        out(f"Fonts: {destinations[0]}")
        return
    out("Font destinations:")
    for dest in destinations:
        out(f"  {dest}")


def decide_candidates(
    candidates: list[Candidate],
    dest: Path,
    duplicate_policy: str,
) -> dict[str, int]:
    exact_index, face_index = installed_font_index()
    content_seen: set[str] = set()
    reserved_targets: dict[Path, str] = {}
    counters: dict[str, int] = {}

    for cand in candidates:
        if cand.state in {"invalid", "malformed", "unsupported", "skip"}:
            counters[cand.reason or cand.state] = counters.get(cand.reason or cand.state, 0) + 1
            continue

        if cand.sha256 in content_seen:
            cand.state = "skip"
            cand.reason = "duplicate-content-in-run"
            counters[cand.reason] = counters.get(cand.reason, 0) + 1
            continue

        target, target_state = choose_target(candidate_dest(cand, dest), cand, reserved_targets)
        cand.target = target
        if target_state == "identical-file" and duplicate_policy != "keep-all":
            cand.state = "skip"
            cand.reason = "identical-file"
            content_seen.add(cand.sha256)
            add_candidate_to_indexes(cand, exact_index, face_index)
            counters[cand.reason] = counters.get(cand.reason, 0) + 1
            continue

        exact_hits = 0
        older_hits = 0
        newer_hits = 0
        partial_hits = 0
        for face in cand.faces:
            if face.exact_key() in exact_index:
                exact_hits += 1
                continue
            existing = face_index.get(face.face_key(), [])
            if existing:
                partial_hits += 1
                existing_revisions = [item.revision for item in existing if item.revision is not None]
                if face.revision is not None and existing_revisions:
                    if face.revision > max(existing_revisions):
                        newer_hits += 1
                    elif face.revision < max(existing_revisions):
                        older_hits += 1

        if exact_hits == len(cand.faces) and duplicate_policy == "skip-existing":
            cand.state = "skip"
            cand.reason = "identical-font-metadata"
        elif older_hits and not newer_hits and duplicate_policy == "skip-existing":
            cand.state = "skip"
            cand.reason = "older-version"
        elif exact_hits and exact_hits < len(cand.faces):
            cand.state = "install"
            cand.reason = "partially-duplicated-collection"
        elif newer_hits:
            cand.state = "install"
            cand.reason = "newer-version"
        elif partial_hits and duplicate_policy == "skip-existing":
            cand.state = "install"
            cand.reason = "different-version-or-face"
        else:
            cand.state = "install"
            cand.reason = target_state

        if cand.state == "install":
            reserved_targets[target] = cand.sha256
        content_seen.add(cand.sha256)
        add_candidate_to_indexes(cand, exact_index, face_index)
        counters[cand.reason] = counters.get(cand.reason, 0) + 1
    return counters


def add_candidate_to_indexes(
    cand: Candidate,
    exact_index: dict[tuple[str, str, str, str, str], list[dict[str, Any]]],
    face_index: dict[tuple[str, str, str], list[Face]],
) -> None:
    for face in cand.faces:
        exact_index.setdefault(face.exact_key(), []).append({"file": str(cand.path), "face": face.to_json()})
        face_index.setdefault(face.face_key(), []).append(face)


def read_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    seen_dest: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WinfontsError(f"manifest is malformed at line {line_no}: {exc}", EXIT_USAGE) from exc
            if record.get("schema") != SCHEMA:
                raise WinfontsError(f"unsupported manifest schema at line {line_no}", EXIT_USAGE)
            if record.get("record") == "font_file":
                dest = str(record.get("dest_path", ""))
                if dest in seen_dest:
                    raise WinfontsError(f"duplicate manifest record for {dest}", EXIT_USAGE)
                seen_dest.add(dest)
            records.append(record)
    return records


def write_manifest_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chown_target_chain_if_sudo(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        chown_target_if_sudo(path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install_candidate(cand: Candidate, dest: Path) -> dict[str, Any]:
    assert cand.target is not None
    target = cand.target
    check_space(dest, cand.size * 2 + 4096, "font installation")
    tmp = dest / f".{target.name}.tmp.{os.getpid()}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    try:
        shutil.copyfile(cand.path, tmp, follow_symlinks=False)
        os.chmod(tmp, 0o644)
        if tmp.stat().st_size != cand.size:
            raise WinfontsError(f"copied size mismatch for {cand.original_filename}", EXIT_IO)
        copied_hash = sha256_file(tmp)
        if copied_hash != cand.sha256:
            raise WinfontsError(f"copied hash mismatch for {cand.original_filename}", EXIT_IO)
        copied_faces = fc_scan(tmp)
        if not copied_faces:
            raise WinfontsError(f"copied font failed validation: {cand.original_filename}", EXIT_IO)
        os.replace(tmp, target)
        chown_target_if_sudo(target)
        return {
            "schema": SCHEMA,
            "record": "font_file",
            "hash_algorithm": HASH_ALGO,
            "sha256": cand.sha256,
            "size": cand.size,
            "original_filename": cand.original_filename,
            "installed_filename": target.name,
            "dest_path": str(target),
            "install_dir": str(dest),
            "source_path": cand.source_path,
            "source_type": cand.source_type,
            "source_stream": cand.source_stream,
            "wim_image_index": cand.wim_image_index,
            "faces": [face.to_json() for face in cand.faces],
            "newly_created": True,
            "decision_reason": cand.reason,
        }
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise WinfontsError("ran out of disk space while copying font", EXIT_IO) from exc
        raise
    finally:
        try:
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass


def rollback_files(records: list[dict[str, Any]]) -> None:
    for record in reversed(records):
        path = Path(str(record.get("dest_path", "")))
        try:
            if path.exists() and not path.is_symlink() and sha256_file(path) == record.get("sha256"):
                path.unlink()
        except OSError:
            pass


def install_command(args: argparse.Namespace) -> int:
    sources = [canonical_existing(Path(item), "source") for item in args.sources]
    if args.dest is not None:
        reject_bad_path_text(args.dest, "--dest")
    if args.manifest is not None:
        reject_bad_path_text(args.manifest, "--manifest")
    if args.image is not None and args.image <= 0:
        raise WinfontsError("--image must be a positive integer", EXIT_USAGE)

    tm = TempManager()
    with tm as tmp:
        detected: list[tuple[Path, SourceInfo]] = []
        for source in sources:
            info = detect_source(source, tmp, tm)
            detected.append((source, info))
        validate_source_options(args, [info for _source, info in detected])
        if target_user_context().from_sudo and args.dest is None:
            ctx = target_user_context()
            log(f"sudo target user: {ctx.username} ({ctx.home})")

        plans: list[SourcePlan] = []
        for source, info in detected:
            dest_arg = Path(args.dest) if args.dest is not None else default_dest_for_source(info)
            if args.dry_run:
                dest = canonical_future_path(dest_arg, "dest")
            else:
                dest = canonical_for_create(dest_arg, "dest", create_parent=True)
            plans.append(SourcePlan(source=source, info=info, dest=dest))

        manifest_arg = Path(args.manifest) if args.manifest is not None else default_manifest()
        if args.dry_run:
            manifest = canonical_future_path(manifest_arg, "manifest")
        else:
            manifest = canonical_for_create(manifest_arg, "manifest", create_parent=True)

        with Lock(manifest.parent / ".lock"):
            old_records = read_manifest(manifest)
            if has_incomplete_transaction(old_records):
                raise WinfontsError("previous installation transaction is incomplete; run status/verify before installing again", EXIT_PARTIAL)
            return _install_with_temp(plans, tmp, manifest, old_records, args)


def validate_source_options(args: argparse.Namespace, infos: list[SourceInfo]) -> None:
    has_office = any(info.source_type.endswith("office-clicktorun") for info in infos)
    if args.office_neutral_only and not has_office:
        raise WinfontsError("--office-x-none-only/--office-neutral-only is only valid when at least one source is Office", EXIT_USAGE)
    if args.office_language and not has_office:
        raise WinfontsError("--office-language is only valid when at least one source is Office", EXIT_USAGE)
    if args.office_arch != "x64" and not has_office:
        raise WinfontsError("--office-arch is only valid when at least one source is Office", EXIT_USAGE)
    if args.image is not None and any(
        not (info.source_type.endswith("windows-image") or info.source_type.endswith("windows-media")) for info in infos
    ):
        raise WinfontsError("--image is only valid for Windows WIM/ESD media", EXIT_USAGE)


def _install_with_temp(
    plans: list[SourcePlan],
    tmp: Path,
    manifest: Path,
    old_records: list[dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    transaction_id = uuid.uuid4().hex
    installed_records: list[dict[str, Any]] = []
    try:
        candidates: list[Candidate] = []
        for index, plan in enumerate(plans, start=1):
            source_tmp = tmp / f"source-{index}"
            source_tmp.mkdir()
            log(f"source {index}/{len(plans)}: {plan.source}")
            log(f"source type: {plan.info.source_type}")
            log(f"font destination: {plan.dest}")
            source_candidates, notes = extract_candidates(
                plan.info,
                source_tmp,
                args.image,
                args.office_neutral_only,
                args.office_arch,
                args.office_language or [],
            )
            for candidate in source_candidates:
                candidate.install_dir = plan.dest
            log(f"source candidates: {len(source_candidates)}")
            candidates.extend(source_candidates)

        if not candidates:
            raise WinfontsError("no font candidates found", EXIT_NO_FONTS)
        log(f"candidate files total: {len(candidates)}")
        build_candidate_metadata(candidates)
        fallback_dest = plans[0].dest
        counters = decide_candidates(candidates, fallback_dest, args.duplicate_policy)
        installable = [candidate for candidate in candidates if candidate.state == "install"]
        destinations = sorted({candidate_dest(candidate, fallback_dest) for candidate in candidates}, key=lambda item: str(item))

        for key in sorted(counters):
            log(f"decision {key}: {counters[key]}")

        if args.dry_run:
            for candidate in installable:
                out(f"would install: {candidate.original_filename} [{candidate.reason}] -> {candidate.target}")
            out(f"Would install: {len(installable)}")
            out(f"Skipped/invalid: {len(candidates) - len(installable)}")
            print_destinations(destinations)
            out(f"Manifest: {manifest}")
            return EXIT_DUPLICATES if not installable else EXIT_OK

        size_by_dest: dict[Path, int] = {}
        count_by_dest: dict[Path, int] = {}
        for candidate in installable:
            dest = candidate_dest(candidate, fallback_dest)
            size_by_dest[dest] = size_by_dest.get(dest, 0) + candidate.size
            count_by_dest[dest] = count_by_dest.get(dest, 0) + 1
        for dest in sorted(size_by_dest, key=lambda item: str(item)):
            dest.mkdir(parents=True, exist_ok=True)
            chown_target_chain_if_sudo(dest)
            check_space(dest, size_by_dest[dest] + 4096 * max(1, count_by_dest[dest]), "font installation")
        manifest_records = old_records + [
            {
                "schema": SCHEMA,
                "record": "transaction",
                "transaction_id": transaction_id,
                "state": "started",
                "time": now_iso(),
                "dest": str(plans[0].dest) if len(destinations) == 1 else "multiple",
                "destinations": [str(dest) for dest in destinations],
                "source": str(plans[0].source) if len(plans) == 1 else "multiple",
                "sources": [str(plan.source) for plan in plans],
            }
        ]
        for candidate in installable:
            record = install_candidate(candidate, candidate_dest(candidate, fallback_dest))
            record["transaction_id"] = transaction_id
            record["installed_at"] = now_iso()
            installed_records.append(record)
            manifest_records.append(record)
        manifest_records.append(
            {
                "schema": SCHEMA,
                "record": "transaction",
                "transaction_id": transaction_id,
                "state": "complete",
                "time": now_iso(),
                "installed_files": len(installed_records),
            }
        )
        try:
            write_manifest_atomic(manifest, manifest_records)
        except Exception:
            rollback_files(installed_records)
            aborted = old_records + [
                {
                    "schema": SCHEMA,
                    "record": "transaction",
                    "transaction_id": transaction_id,
                    "state": "aborted",
                    "time": now_iso(),
                    "reason": "manifest-write-failed",
                }
            ]
            try:
                write_manifest_atomic(manifest, aborted)
            except Exception:
                pass
                raise
        for dest in destinations:
            run(["fc-cache", "-f", "--", str(dest)], capture=True, check=False, as_target_user=True)
        out(f"Installed: {len(installed_records)}")
        out(f"Skipped/invalid: {len(candidates) - len(installed_records)}")
        print_destinations(destinations)
        out(f"Manifest: {manifest}")
        out(f"Rollback: ./winfonts uninstall --manifest {manifest}")
        return EXIT_DUPLICATES if not installed_records else EXIT_OK
    except Exception:
        rollback_files(installed_records)
        raise


def has_incomplete_transaction(records: list[dict[str, Any]]) -> bool:
    states: dict[str, str] = {}
    for record in records:
        if record.get("record") == "transaction":
            states[str(record.get("transaction_id"))] = str(record.get("state"))
    return any(state == "started" for state in states.values())


def verify_records(manifest: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records = read_manifest(manifest)
    counters = {"ok": 0, "missing": 0, "modified": 0, "malformed": 0, "symlink": 0}
    for record in records:
        if record.get("record") != "font_file":
            continue
        path = Path(str(record.get("dest_path", "")))
        try:
            if "\n" in str(path) or "\t" in str(path):
                record["_status"] = "malformed"
                counters["malformed"] += 1
                continue
            if path.is_symlink():
                record["_status"] = "symlink"
                counters["symlink"] += 1
                continue
            if not path.exists():
                record["_status"] = "missing"
                counters["missing"] += 1
                continue
            if sha256_file(path) != record.get("sha256"):
                record["_status"] = "modified"
                counters["modified"] += 1
                continue
            record["_status"] = "ok"
            counters["ok"] += 1
        except OSError:
            record["_status"] = "malformed"
            counters["malformed"] += 1
    return records, counters


def status_command(args: argparse.Namespace) -> int:
    if args.manifest is not None:
        reject_bad_path_text(args.manifest, "--manifest")
    manifest_arg = Path(args.manifest) if args.manifest is not None else default_manifest()
    manifest = canonical_for_create(manifest_arg, "manifest", create_parent=True)
    if not manifest.exists():
        out(f"No manifest found: {manifest}")
        return EXIT_OK
    with Lock(manifest.parent / ".lock"):
        _records, counters = verify_records(manifest)
    out(f"Manifest: {manifest}")
    for key in ("ok", "missing", "modified", "symlink", "malformed"):
        out(f"{key}: {counters[key]}")
    return EXIT_VERIFY if any(counters[key] for key in ("modified", "symlink", "malformed")) else EXIT_OK


def uninstall_command(args: argparse.Namespace) -> int:
    if args.manifest is not None:
        reject_bad_path_text(args.manifest, "--manifest")
    manifest_arg = Path(args.manifest) if args.manifest is not None else default_manifest()
    manifest = canonical_for_create(manifest_arg, "manifest", create_parent=True)
    if not manifest.exists():
        raise WinfontsError(f"manifest not found: {manifest}", EXIT_NOT_FOUND)
    with Lock(manifest.parent / ".lock"):
        records, counters = verify_records(manifest)
        remaining: list[dict[str, Any]] = []
        removed = 0
        blocked = 0
        for record in records:
            if record.get("record") != "font_file":
                remaining.append(record)
                continue
            status = record.get("_status")
            path = Path(str(record.get("dest_path", "")))
            install_dir = Path(str(record.get("install_dir", ""))).resolve(strict=False)
            resolved = path.resolve(strict=False)
            if not str(resolved).startswith(str(install_dir) + os.sep):
                record["_uninstall_status"] = "path-escape"
                remaining.append(record)
                blocked += 1
                continue
            if not record.get("newly_created", False):
                record["_uninstall_status"] = "not-created-by-transaction"
                remaining.append(record)
                blocked += 1
                continue
            if status != "ok":
                record["_uninstall_status"] = status
                remaining.append(record)
                blocked += 1
                continue
            if args.dry_run:
                out(f"would remove: {path}")
                remaining.append(record)
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                record["_uninstall_status"] = f"remove-failed:{exc.errno}"
                remaining.append(record)
                blocked += 1
        if args.dry_run:
            out(f"Would remove: {sum(1 for r in records if r.get('record') == 'font_file' and r.get('_status') == 'ok')}")
            return EXIT_OK
        font_records_left = [r for r in remaining if r.get("record") == "font_file"]
        if font_records_left:
            clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in remaining]
            write_manifest_atomic(manifest, clean)
        else:
            try:
                manifest.unlink()
            except FileNotFoundError:
                pass
        run(["fc-cache", "-f"], capture=True, check=False, as_target_user=True)
        out(f"Removed: {removed}")
        out(f"Blocked: {blocked}")
        if font_records_left:
            out(f"Manifest kept: {manifest}")
            return EXIT_PARTIAL
        out(f"Deleted: {manifest}")
        return EXIT_OK


def scan_command(args: argparse.Namespace) -> int:
    args.dry_run = True
    return install_command(args)


def images_command(args: argparse.Namespace) -> int:
    source = canonical_existing(Path(args.source), "source")
    tm = TempManager()
    with tm as tmp:
        info = detect_source(source, tmp, tm)
        if not (info.source_type.endswith("windows-image") or info.source_type.endswith("windows-media")):
            raise WinfontsError("source is not Windows WIM/ESD media; Office and loose-font sources do not have image indexes", EXIT_USAGE)
        assert info.wim is not None
        proc = run(["wimlib-imagex", "info", str(info.wim)], capture=True)
        out(proc.stdout.rstrip())
        return EXIT_OK


def doctor_command(_args: argparse.Namespace) -> int:
    required = ["python3", "fc-scan", "fc-list", "fc-cache", "mount", "umount"]
    ok = True
    for cmd in required:
        exists = command_exists(cmd)
        out(f"{'ok' if exists else 'missing'}      {cmd}")
        ok = ok and exists
    wim = command_exists("wimlib-imagex")
    out(f"{'ok' if wim else 'missing'}      wimlib-imagex")
    ok = ok and wim
    out(f"{'ok' if OFFICE_CARVER.exists() else 'missing'}      {OFFICE_CARVER}")
    ok = ok and OFFICE_CARVER.exists()
    out("ok      sha256")
    return EXIT_OK if ok else EXIT_USAGE


def positive_int(value: str) -> int:
    if not value or not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


COMMAND_NAMES = (
    "install",
    "add",
    "scan",
    "preview",
    "dry-run",
    "images",
    "list-images",
    "editions",
    "status",
    "verify",
    "uninstall",
    "rollback",
    "remove",
    "doctor",
    "check",
    "paths",
    "where",
    "version",
)


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


class FriendlyParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "invalid choice" in message and "COMMAND" in message:
            bad = ""
            match = re.search(r"invalid choice: '([^']+)'", message)
            if match:
                bad = match.group(1)
            suggestion = difflib.get_close_matches(bad, COMMAND_NAMES, n=1)
            self.print_usage(sys.stderr)
            hint = f"\nDid you mean '{suggestion[0]}'?\n" if suggestion else ""
            self.exit(
                EXIT_USAGE,
                f"{self.prog}: unknown command '{bad}'{hint}\n"
                f"Common commands: install, scan, status, uninstall, doctor, paths\n"
                f"Run '{self.prog} --help' for examples.\n",
            )
        super().error(message)


def display_prog() -> str:
    raw = os.environ.get("WINFONTS_PROG", APP)
    if raw.startswith("./") or raw.startswith("../"):
        return raw
    name = Path(raw).name
    return name or APP


def version_command(_args: argparse.Namespace) -> int:
    out(f"{display_prog()} {VERSION}")
    return EXIT_OK


def paths_command(args: argparse.Namespace) -> int:
    if args.dest is not None:
        reject_bad_path_text(args.dest, "--dest")
    ctx = target_user_context()
    if ctx.from_sudo:
        out(f"Target user: {ctx.username} ({ctx.home})")
    out(f"Windows fonts: {default_fonts_base() / 'windows'}")
    out(f"Office fonts: {default_fonts_base() / 'office'}")
    out(f"Loose fonts: {default_fonts_base() / 'loose'}")
    out(f"Manifest: {default_manifest()}")

    if not args.sources:
        return EXIT_OK

    tm = TempManager()
    with tm as tmp:
        for raw_source in args.sources:
            source = canonical_existing(Path(raw_source), "source")
            info = detect_source(source, tmp, tm)
            dest_arg = Path(args.dest) if args.dest is not None else default_dest_for_source(info)
            dest = canonical_future_path(dest_arg, "dest")
            out("")
            out(f"Source: {source}")
            out(f"Type: {info.source_type}")
            out(f"Destination: {dest}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = FriendlyParser(
        prog=display_prog(),
        formatter_class=HelpFormatter,
        description=(
            "Extract Microsoft fonts from one or more Windows/Office sources and install only "
            "the fonts missing from this Linux user account."
        ),
        epilog="""\
Common commands:
  ./winfonts doctor
      Check required tools.

  ./winfonts paths
      Show install folders and manifest path.

  ./winfonts install Windows.iso --dry-run
      Preview fonts from a Windows ISO without writing font files.

  ./winfonts install /run/media/$USER/16.0.17928.20148 --office-x-none-only --dry-run
      Preview fonts from a mounted Office IMG.

  ./winfonts install Office.img --office-x-none-only
      Install Office fonts from an IMG file. If mount is denied, retry with sudo.

  ./winfonts scan Office1.img Office2.img --office-x-none-only
      Preview multiple sources together and deduplicate across all of them.

  ./winfonts images Windows.iso
      Optional: list Windows editions if you want to override the default image index.

  ./winfonts status
      Show whether installed files still match the manifest.

  ./winfonts uninstall --dry-run
      Preview rollback of fonts installed by this tool.

Default install folders:
  Windows sources: ~/.local/share/fonts/microsoft-fonts/windows
  Office sources:  ~/.local/share/fonts/microsoft-fonts/office
  Manifest:        ~/.local/state/microsoft-fonts/manifest.jsonl

Use "./winfonts COMMAND --help" for command-specific options.
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
        help="show version and exit",
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="COMMAND",
        title="commands",
        description="Run one of these commands:",
        parser_class=FriendlyParser,
        required=False,
    )

    install = sub.add_parser(
        "install",
        aliases=("add",),
        formatter_class=HelpFormatter,
        help="install missing fonts from a Windows/Office source",
        description="Extract, validate, deduplicate, and install fonts from one or more sources.",
        epilog="""\
Examples:
  ./winfonts install Windows.iso
  ./winfonts install Windows.iso --dry-run
  ./winfonts install Office1.img Office2.img --office-x-none-only --dry-run
  ./winfonts install Office.img --office-x-none-only
  ./winfonts install /run/media/$USER/16.0.17928.20148 --office-x-none-only
  sudo ./winfonts install Office.img --office-x-none-only
""",
    )
    add_source_decision_args(install, include_dry_run=True)
    install.set_defaults(func=install_command)

    scan = sub.add_parser(
        "scan",
        aliases=("preview", "dry-run"),
        formatter_class=HelpFormatter,
        help="dry-run install decisions without writing font files",
        description="Scan one or more sources and show the same decisions install would make, without installing.",
        epilog="""\
Examples:
  ./winfonts scan Windows.iso
  ./winfonts scan Office.img --office-x-none-only
  ./winfonts scan Office1.img Office2.img --office-x-none-only
  ./winfonts scan /path/to/Windows/Fonts
""",
    )
    add_source_decision_args(scan, include_dry_run=False)
    scan.set_defaults(func=scan_command)

    images = sub.add_parser(
        "images",
        aliases=("list-images", "editions"),
        formatter_class=HelpFormatter,
        help="list Windows WIM/ESD image indexes",
        description="Show Windows editions inside setup media. Optional; install defaults to image index 1.",
        epilog="""\
Examples:
  ./winfonts images Windows.iso
  ./winfonts images /mnt/windows-iso
  ./winfonts images sources/install.wim
""",
    )
    images.add_argument(
        "source",
        metavar="SOURCE",
        help="Windows ISO/IMG, mounted Windows media, install.wim, or install.esd",
    )
    images.set_defaults(func=images_command)

    status = sub.add_parser(
        "status",
        formatter_class=HelpFormatter,
        help="show recorded install state",
        description="Verify the manifest and summarize installed, missing, modified, or unsafe files.",
    )
    status.add_argument(
        "--manifest",
        metavar="PATH",
        help="manifest to inspect",
    )
    status.set_defaults(func=status_command)

    verify = sub.add_parser(
        "verify",
        formatter_class=HelpFormatter,
        help="same check as status",
        description="Alias-style command for status; verifies installed files against the manifest.",
    )
    verify.add_argument(
        "--manifest",
        metavar="PATH",
        help="manifest to inspect",
    )
    verify.set_defaults(func=status_command)

    uninstall = sub.add_parser(
        "uninstall",
        aliases=("rollback", "remove"),
        formatter_class=HelpFormatter,
        help="remove fonts installed by this tool",
        description="Rollback files recorded in the manifest, after verifying their hashes.",
        epilog="""\
Examples:
  ./winfonts uninstall --dry-run
  ./winfonts uninstall
  ./winfonts uninstall --manifest ~/.local/state/microsoft-fonts/manifest.jsonl
""",
    )
    uninstall.add_argument(
        "--manifest",
        metavar="PATH",
        help="manifest to use for rollback",
    )
    uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="show files that would be removed without deleting anything",
    )
    uninstall.set_defaults(func=uninstall_command)

    doctor = sub.add_parser(
        "doctor",
        aliases=("check",),
        formatter_class=HelpFormatter,
        help="check dependencies",
        description="Check required command-line tools and bundled helper scripts.",
    )
    doctor.set_defaults(func=doctor_command)

    paths = sub.add_parser(
        "paths",
        aliases=("where",),
        formatter_class=HelpFormatter,
        help="show default folders",
        description="Show default install folders and, optionally, the destination chosen for each source.",
        epilog="""\
Examples:
  ./winfonts paths
  ./winfonts paths Windows.iso Office.img
  ./winfonts where /run/media/$USER/16.0.17928.20148
""",
    )
    paths.add_argument(
        "sources",
        metavar="SOURCE",
        nargs="*",
        help="optional source paths to classify and map to destinations",
    )
    paths.add_argument(
        "-o",
        "--dest",
        metavar="DIR",
        help="show where sources would install with this custom destination",
    )
    paths.set_defaults(func=paths_command)

    version = sub.add_parser(
        "version",
        formatter_class=HelpFormatter,
        help="show version",
        description="Show the winfonts version.",
    )
    version.set_defaults(func=version_command)
    return parser


def add_source_decision_args(parser: argparse.ArgumentParser, *, include_dry_run: bool) -> None:
    parser.add_argument(
        "sources",
        metavar="SOURCE",
        nargs="+",
        help=(
            "one or more Windows ISO/IMG files, mounted Windows media, install.wim/esd files, "
            "Windows/Fonts directories, Office IMG/mounts, or loose font directories"
        ),
    )
    parser.add_argument(
        "-i",
        "--image",
        metavar="N",
        type=positive_int,
        help="advanced Windows override: WIM/ESD image index; default is 1",
    )
    parser.add_argument(
        "-o",
        "--dest",
        metavar="DIR",
        help=(
            "override font install directory; default is source-aware under "
            "~/.local/share/fonts/microsoft-fonts"
        ),
    )
    parser.add_argument(
        "-m",
        "--manifest",
        metavar="PATH",
        help="override manifest path",
    )
    if include_dry_run:
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="show decisions and target paths without installing fonts",
        )
    else:
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="accepted for consistency; scan/preview never installs fonts",
        )
    parser.add_argument(
        "--office-x-none-only",
        "--office-neutral-only",
        dest="office_neutral_only",
        action="store_true",
        help="Office only: scan language-neutral x-none streams, usually fastest",
    )
    parser.add_argument(
        "--office-arch",
        choices=("x64", "x86", "all"),
        default="x64",
        help="Office only: stream architecture to scan; default is x64",
    )
    parser.add_argument(
        "--office-language",
        metavar="TAG",
        action="append",
        default=[],
        help="Office only: include a language stream such as en-us; repeatable",
    )
    parser.add_argument(
        "--duplicate-policy",
        choices=("skip-existing", "prefer-newer", "prefer-source", "keep-all"),
        default="skip-existing",
        help="how to handle fonts that match already installed metadata/content; default is skip-existing",
    )
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK
    if getattr(args, "force", False):
        args.duplicate_policy = "keep-all"
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_INTERRUPTED
    except WinfontsError as exc:
        log(f"{APP}: {exc}")
        return exc.code
    except PermissionError as exc:
        path = getattr(exc, "filename", None)
        suffix = f": {path}" if path else ""
        log(f"{APP}: permission denied{suffix}: {exc.strerror or exc}")
        if os.geteuid() != 0:
            log(f"{APP}: retry with sudo when accessing protected images, mount points, or destinations")
        return EXIT_IO
    except OSError as exc:
        log(f"{APP}: operating-system error: {exc}")
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
