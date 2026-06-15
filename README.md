# winfonts

`winfonts` extracts Microsoft fonts from Windows and Office media on Linux. It
installs only missing fonts, records every installed file in a manifest, and can
roll the install back safely.

## Quick Start

Check dependencies:

```sh
./winfonts doctor
```

Preview an Office IMG without installing anything:

```sh
./winfonts scan /path/to/Office.img --office-x-none-only
```

Install from an Office IMG:

```sh
./winfonts install /path/to/Office.img --office-x-none-only
```

Preview your mounted Office IMG:

```sh
./winfonts scan /run/media/milenko/16.0.17928.20148 --office-x-none-only
```

Install from a Windows ISO:

```sh
./winfonts install /path/to/Windows.iso
```

Rollback:

```sh
./winfonts uninstall --dry-run
./winfonts uninstall
```

## Common Commands

```sh
./winfonts paths
./winfonts scan SOURCE [SOURCE ...]
./winfonts install SOURCE [SOURCE ...]
./winfonts status
./winfonts uninstall
```

Useful aliases:

```sh
./winfonts preview SOURCE
./winfonts dry-run SOURCE
./winfonts rollback
./winfonts check
./winfonts where
```

## Supported Sources

You can pass one or more sources in the same command:

- Windows ISO/IMG files
- Mounted Windows setup media
- `install.wim` or `install.esd`
- An installed Windows partition containing `Windows/Fonts`
- Office IMG files
- Mounted Office Click-to-Run media
- Loose directories containing `.ttf`, `.otf`, `.ttc`, or `.otc` fonts

Examples:

```sh
./winfonts scan Office1.img Office2.img --office-x-none-only
./winfonts install Windows.iso Office.img --office-x-none-only --dry-run
```

All candidates from all sources are deduplicated together before installation.
Windows fonts go to the Windows folder, Office fonts go to the Office folder,
unless you override the destination with `--dest`.

## Dry Run

Use `scan`, `preview`, `dry-run`, or `install --dry-run`:

```sh
./winfonts scan /path/to/Office.img --office-x-none-only
./winfonts install /path/to/Office.img --office-x-none-only --dry-run
./winfonts install /path/to/Office.img --office-x-none-only -n
```

Dry run prints every font that would be installed:

```text
would install: Calibri.ttf [new-file] -> /home/me/.local/share/fonts/microsoft-fonts/office/...
Would install: 12
Skipped/invalid: 40
```

## Default Paths

Show paths:

```sh
./winfonts paths
```

Defaults:

```text
Windows fonts: ~/.local/share/fonts/microsoft-fonts/windows
Office fonts:  ~/.local/share/fonts/microsoft-fonts/office
Loose fonts:   ~/.local/share/fonts/microsoft-fonts/loose
Manifest:      ~/.local/state/microsoft-fonts/manifest.jsonl
```

Show where specific sources would install:

```sh
./winfonts paths Windows.iso Office.img
```

Override paths:

```sh
./winfonts install Office.img \
  --office-x-none-only \
  --dest "$HOME/.local/share/fonts/microsoft-fonts/office" \
  --manifest "$HOME/.local/state/microsoft-fonts/manifest.jsonl"
```

Short options:

```text
-n, --dry-run       Preview only.
-o, --dest DIR      Override the font destination.
-m, --manifest PATH Override the manifest path.
-i, --image N       Windows WIM/ESD image index override.
```

## Office Options

Recommended first pass:

```sh
./winfonts scan Office.img --office-x-none-only
```

Useful filters:

```sh
./winfonts scan Office.img --office-x-none-only --office-arch x64
./winfonts scan Office.img --office-language en-us
```

`--office-x-none-only` scans language-neutral streams first. It is usually much
faster and catches the shared Office font resources.

## Windows Options

Windows setup media defaults to image index `1`, which is normally enough for
font extraction:

```sh
./winfonts install Windows.iso
```

If you need a specific Windows edition inside the ISO:

```sh
./winfonts images Windows.iso
./winfonts install Windows.iso --image N
```

## Sudo And Mounting

For ISO/IMG files, `winfonts` tries a read-only loop mount. Some distros require
root for that:

```sh
sudo ./winfonts scan Office.img --office-x-none-only
sudo ./winfonts install Windows.iso
```

When run with `sudo`, defaults still target the original `SUDO_USER` home, not
`/root`.

You can also mount manually:

```sh
sudo mount -o loop,ro Office.img /mnt/office
./winfonts scan /mnt/office --office-x-none-only
sudo umount /mnt/office
```

## Safety

- Installs into per-user font directories by default.
- Does not modify system font directories.
- Copies through a temporary file and atomically renames after validation.
- Records installed files in a JSON Lines manifest.
- Uninstall verifies hashes before deleting files.
- Modified or unexpected files are not deleted during rollback.
- A lock prevents install and uninstall from running at the same time.

## Requirements

- `python3`
- `fontconfig`: `fc-scan`, `fc-list`, `fc-cache`
- `mount` and `umount` for direct ISO/IMG input
- `wimlib-imagex` for Windows ISO/WIM/ESD sources

Office extraction uses Python's standard library and the bundled
`office_font_carver.py`.

## Command Reference

```text
install     Install missing fonts from one or more sources.
scan        Dry-run install decisions without writing font files.
preview     Alias for scan.
dry-run     Alias for scan.
images      List Windows WIM/ESD image indexes.
status      Verify installed files against the manifest.
verify      Alias-style status check.
uninstall   Roll back installed fonts.
rollback    Alias for uninstall.
doctor      Check dependencies.
check       Alias for doctor.
paths       Show default folders and source destinations.
where       Alias for paths.
version     Show version.
```

Run command-specific help:

```sh
./winfonts install --help
./winfonts scan --help
```

## License Note

This tool does not download or redistribute fonts. It only extracts fonts from
media you provide. Make sure your Windows or Office license allows your use.
