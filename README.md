# winfonts

`winfonts` extracts Microsoft fonts from Windows and Office media on Linux. It
validates every font, skips duplicates, installs readable filenames, records
what it changed, and can safely undo the installation later.

It does not download or redistribute fonts. You provide the Windows or Office
media and remain responsible for complying with its license.

## Quick start

Make the launcher executable after downloading or cloning:

```sh
chmod +x winfonts
```

Open the guided menu:

```sh
./winfonts
```

The menu can scan, install, list, verify, and uninstall fonts without requiring
you to remember command-line options. It is also available explicitly:

```sh
./winfonts interactive
./winfonts menu
./winfonts wizard
```

For command-line use:

```sh
./winfonts doctor
./winfonts scan /path/to/Office.img --office-x-none-only
./winfonts install /path/to/Office.img --office-x-none-only
./winfonts install /path/to/Windows.iso --source-sha256 EXPECTED_SHA256
./winfonts list
./winfonts status
./winfonts repair --dry-run
./winfonts uninstall --dry-run
./winfonts uninstall
```

## Commands

| Command | Purpose |
| --- | --- |
| `interactive` | Open the guided menu. This is automatic when no command is given in a terminal. |
| `doctor` | Check core and source-specific dependencies. |
| `scan SOURCE...` | Preview extraction and installation without writing font files. |
| `install SOURCE...` | Extract, validate, deduplicate, and install fonts. |
| `images SOURCE` | List Windows editions contained in WIM/ESD media. |
| `list` | List font files managed by `winfonts` and verify each one. |
| `status` | Summarize valid, missing, modified, or unsafe managed files. |
| `repair` | Safely forget invalid records or compact an inconsistent manifest. |
| `paths [SOURCE...]` | Show default paths and source-specific destinations. |
| `uninstall` | Remove files safely using the installation manifest. |
| `help [COMMAND]` | Show general or command-specific help. |
| `version` | Show the installed version. |

Useful aliases:

```text
add                         install
preview, dry-run            scan
list-images, editions       images
installed                   list
verify                      status
rollback, remove            uninstall
fix-manifest                repair
check                       doctor
where                       paths
menu, wizard                interactive
```

Detailed help is available in either style:

```sh
./winfonts help install
./winfonts install --help
```

Misspelled commands receive a suggestion when a close match exists.

## Interactive mode

Run `./winfonts` in a terminal and choose an action from the menu. The wizard:

- accepts paths containing spaces;
- supports multiple sources in one run;
- offers the recommended fast Office scan;
- exposes destination, manifest, architecture, language, and image-index
  options only when requested;
- asks for confirmation before installing;
- always previews an uninstall before asking for confirmation.

Regular non-interactive commands retain their existing behavior, making them
safe to use in scripts.

## Supported sources

One command may receive one or more sources:

- Windows ISO or IMG files;
- mounted Windows setup media;
- `install.wim`, `install.esd`, or a directory containing either;
- an installed Windows partition containing `Windows/Fonts`;
- Office Click-to-Run IMG files or mounted media;
- legacy Office media containing CAB or MSI payloads;
- directories containing loose `.ttf`, `.otf`, `.ttc`, or `.otc` files.

All candidates from all sources are deduplicated together:

```sh
./winfonts scan Windows.iso Office.img --office-x-none-only
./winfonts install Office1.img Office2.img --office-x-none-only
```

Windows, Office, and loose-font sources use separate destination folders unless
`--dest` overrides them.

## Preview before installing

`scan` makes the same extraction and duplicate decisions as `install`, but does
not write font files or update the manifest:

```sh
./winfonts scan /path/to/Windows.iso
./winfonts scan /path/to/Office.img --office-x-none-only
./winfonts install /path/to/source --dry-run
```

Example:

```text
would install: ba67safs67d6asd6732h23f7uhn2809vgh29.ttf [new-file] -> /home/me/.local/share/fonts/microsoft-fonts/office/Aptos-Bold.ttf
Would install: 1
Skipped/invalid: 0
```

Readable source filenames are preserved. If Windows or Office exposes an opaque
resource identifier, `winfonts` derives a filename from the font's internal
family and style metadata. A short hash is added only when two different files
would otherwise use the same filename.

## Office media

For modern Click-to-Run media, the recommended first pass scans
language-neutral Office resources:

```sh
./winfonts scan Office.img --office-x-none-only
./winfonts install Office.img --office-x-none-only
```

Useful Office options:

```sh
./winfonts scan Office.img --office-arch x64
./winfonts scan Office.img --office-arch all
./winfonts scan Office.img --office-language en-us
```

- `--office-x-none-only` usually finds shared Office fonts much faster.
- `--office-arch` accepts `x64`, `x86`, or `all`; its default is `x64`.
- `--office-language TAG` may be repeated to include language-specific streams.

Legacy Office media is handled separately. If a source contains recognizable
Office CAB or MSI payloads, `winfonts` uses `cabextract` and/or `msiextract`
without executing Windows installers:

```sh
./winfonts scan /path/to/legacy-office-media
./winfonts install /path/to/legacy-office-media
```

Nested CAB/MSI payloads are processed recursively with content deduplication
and a safety limit. Individual extraction failures are reported with the
payload path and tool output while other payloads continue.

Loose font files found in modern Office media are copied before opaque
Click-to-Run streams are carved.

## Windows media

Windows setup media defaults to image index `1`. Most Windows fonts are shared
between editions, so this is normally enough:

```sh
./winfonts scan Windows.iso
./winfonts install Windows.iso
```

To select another edition:

```sh
./winfonts images Windows.iso
./winfonts install Windows.iso --image 6
```

`wimlib-imagex` is required for Windows ISO, WIM, and ESD sources but is not
required for Office or loose-font directories.

To verify official media before extraction and retain the verified digest in
the manifest:

```sh
EXPECTED_SHA256="replace-with-the-64-hex-digit-published-checksum"
./winfonts install Windows.iso --source-sha256 "$EXPECTED_SHA256"
```

For multiple media files, repeat the option and identify each source:

```sh
./winfonts install Windows.iso Office.img \
  --source-sha256 "Windows.iso=$WINDOWS_SHA256" \
  --source-sha256 "Office.img=$OFFICE_SHA256"
```

Only regular-file sources can be hashed; mounted directories cannot.

## Inspecting managed fonts

List every managed file:

```sh
./winfonts list
```

Example:

```text
[ok] Aptos Bold -> /home/me/.local/share/fonts/microsoft-fonts/office/Aptos-Bold.ttf
Total: 1
```

Show only status totals:

```sh
./winfonts status
```

Both commands support JSON for scripts:

```sh
./winfonts list --json
./winfonts status --json
```

Possible verification states include `ok`, `missing`, `modified`, `symlink`,
and `malformed`.

Installation requires a clean manifest. If `status` reports any invalid state,
`install` stops before extraction or copying so a missing file cannot be
reinstalled over a stale record.

## Repairing the manifest

Preview every applicable repair:

```sh
./winfonts repair --dry-run
```

Apply only explicitly selected changes:

```sh
./winfonts repair --drop-missing --compact
./winfonts repair --drop-modified --drop-symlink --compact
./winfonts repair --recover-pending
```

Normal repair options modify only the manifest and never alter or follow a font
file or symlink. `--recover-pending` may remove a regular file
listed in an interrupted-install journal when its SHA-256 still matches the
planned copy. Already committed files and changed files are preserved.
`--compact` resolves duplicate destination records by keeping the newest record
whose file still verifies, then removes transaction records no longer
referenced by a managed font.

## Uninstalling

Always preview first:

```sh
./winfonts uninstall --dry-run
```

Then remove the managed files:

```sh
./winfonts uninstall
```

For a custom manifest:

```sh
./winfonts uninstall --manifest /path/to/manifest.jsonl
```

Uninstall verifies every file hash. Missing, modified, unsafe, or unexpected
files are not deleted; their records remain in the manifest for inspection.

## Paths and custom destinations

Show defaults:

```sh
./winfonts paths
```

Default per-user locations:

```text
Windows fonts: ~/.local/share/fonts/microsoft-fonts/windows
Office fonts:  ~/.local/share/fonts/microsoft-fonts/office
Loose fonts:   ~/.local/share/fonts/microsoft-fonts/loose
Manifest:      ~/.local/state/microsoft-fonts/manifest.jsonl
```

Classify sources and show where each would install:

```sh
./winfonts paths Windows.iso Office.img
```

Override the destination or manifest:

```sh
./winfonts install Office.img \
  --office-x-none-only \
  --dest "$HOME/.local/share/fonts/my-office-fonts" \
  --manifest "$HOME/.local/state/winfonts-office.jsonl"
```

Common short options:

```text
-n, --dry-run       Preview only.
-o, --dest DIR      Override the font destination.
-m, --manifest PATH Override the manifest path.
-i, --image N       Override the Windows WIM/ESD image index.
```

Duplicate policy options:

```sh
./winfonts install SOURCE --duplicate-policy skip-existing
./winfonts install SOURCE --duplicate-policy prefer-newer
./winfonts install SOURCE --duplicate-policy install-source
./winfonts install SOURCE --duplicate-policy keep-all
```

The default is `skip-existing`.

- `skip-existing` skips identical metadata and older versions.
- `prefer-newer` installs only new faces or newer revisions.
- `install-source` installs the supplied source side-by-side unless its exact
  file content already exists at the selected target. The old name
  `prefer-source` remains as a deprecated alias.
- `keep-all` preserves exact duplicates too, always chooses new filenames, and
  never overwrites a pre-existing file.

## Dependencies

Check the current machine:

```sh
./winfonts doctor
```

Core requirements:

- Python 3;
- Fontconfig tools: `fc-scan`, `fc-list`, and `fc-cache`;
- the bundled `office_font_carver.py`.

Source-specific tools:

- `mount` and `umount` when opening ISO/IMG files directly;
- `7z`, `7zz`, or `7za` as a non-root extraction fallback when loop mounting is
  unavailable;
- `wimlib-imagex` for Windows ISO/WIM/ESD sources.
- `cabextract` and `msiextract` for legacy Office media.

Missing source-specific tools are reported as optional because Office media,
mounted directories, and loose-font folders may still work.

## Mount permissions and sudo

Some distributions restrict loop mounting to root:

```sh
sudo ./winfonts scan Office.img --office-x-none-only
sudo ./winfonts install Windows.iso
```

If 7-Zip is installed, `winfonts` automatically falls back to extracting an
ISO/IMG into a temporary directory when a read-only loop mount is unavailable.
It lists the image first, checks free space, and extracts only the relevant
Windows WIM/ESD, Office streams, legacy payloads, or loose fonts when the media
layout is recognizable. Unknown layouts use a checked full-image fallback.

When run through `sudo`, `winfonts` targets the original `SUDO_USER` home rather
than `/root`.

You may also mount media yourself:

```sh
sudo mount -o loop,ro Office.img /mnt/office
./winfonts scan /mnt/office --office-x-none-only
sudo umount /mnt/office
```

If the project was copied from Windows and `./winfonts` reports `sh\r`, ensure
Git respects the included `.gitattributes`, or run:

```sh
git add --renormalize winfonts
git checkout -- winfonts
chmod +x winfonts
```

## Safety model

- Default installs are per-user, not system-wide.
- Sources are opened read-only.
- Symlinked source candidates and unsafe archive paths are rejected.
- Fonts are validated with Fontconfig before and after copying.
- Copies use a verified temporary file and an atomic create-only hard link, so
  a target created after planning is never overwritten.
- Content hashes prevent accidental duplicate installation.
- `keep-all` never reuses or overwrites an existing pathname.
- Planned filenames are reserved to prevent same-run overwrites.
- Every installed file is recorded in a JSON Lines manifest.
- `--source-sha256` verifies media before extraction and records its provenance.
- Uninstall verifies hashes before deleting anything.
- A lock prevents install, uninstall, and status operations from racing.
- Destination-specific locks also serialize operations that use different
  manifests but write to the same font directory.
- Failed transactions roll back files already copied by that transaction.
- Before copying, installs persist a pending journal containing every planned
  destination and SHA-256. After an abrupt process or power failure,
  `repair --recover-pending` removes only matching uncommitted files.
- A failed manifest write always returns an error after rollback, even when the
  aborted transaction can be recorded successfully.
- Duplicate-only installs do not add empty transactions to the manifest.
- Manifest renames and font-directory mutations are followed by directory
  `fsync` calls for stronger Linux crash durability.

## Licensing and redistribution

Use media obtained from Microsoft or your own legitimately licensed
installation. Keep extracted proprietary fonts local to the licensed machine.
Do not commit them to Git, publish them in packages or containers, place them
on shared servers, or convert them to another font format as a redistribution
workaround. Font embedding in documents is a separate right controlled by each
font's OpenType embedding flags.

[Microsoft's font redistribution FAQ](https://learn.microsoft.com/en-us/typography/fonts/font-faq)
specifically states that Segoe UI Variable is not licensed for use outside
Microsoft products or on non-Windows platforms. For document compatibility
without proprietary binaries, consider metric-compatible
[Liberation fonts](https://github.com/liberationfonts/liberation-fonts).
This project is an extraction tool, not legal advice.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `2` | Invalid command, option, source type, or missing core dependency |
| `3` | Required source or manifest not found |
| `4` | No fonts found |
| `5` | Nothing new was installed because candidates were duplicates |
| `6` | Partial operation or incomplete transaction |
| `7` | Verification found modified or unsafe files |
| `8` | Another operation holds the lock |
| `10` | Filesystem or I/O failure |
| `130` | Interrupted |

## Development

Run the test suite on Linux:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile winfonts_engine.py office_font_carver.py
```
