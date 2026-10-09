# `custom/lib/7zip` - bundled decompressor for ZStandard-compressed `.blend` files

Blender 5.2 saves `.blend` files ZStandard-compressed by default (`use_file_compression = True`;
4.5 and earlier default to off), and a compressed stream cannot be seeked, so
`custom/lib/blend_names.py` must decompress the file before the parser can read its block
index. This folder makes that work **without installing anything into Deadline's Python and
without starting Blender** - on Windows, Linux and (with one extra step) macOS.

## What is here

| File | Platform | Size | Why this one |
| --- | --- | --- | --- |
| `7za.exe` | Windows x64 | 1.8 MB | single self-contained file, zstd codec built in |
| `7za-linux-x64` | Linux x86_64 | 2.6 MB | same, ELF x86_64 |
| `License.txt`, `readme.txt`, `History.txt` | - | - | licence and provenance |

`blend_names.py` picks the file for the machine it runs on (`platform_key()`), and only that
platform's files are requested from the repository, so a Windows client never pulls the Linux
binary.

## Why `7za` and not the others

Measured against the original 7-Zip-zstd folder, on both platforms, by decompressing a
Blender 5.2 file (2.7 MB, **19 separate zstd frames**) and comparing the result byte for byte
with Python's own `compression.zstd`:

| Binary | Works? | Notes |
| --- | --- | --- |
| `7za.exe` / `7za` | **yes** | single file, 1.8 / 2.6 MB, zstd codec built in - **this is what is bundled** |
| `7z.exe` + `7z.dll` | yes | two files, 5.7 MB, works but 4× bigger |
| `7zz` (Linux) | yes | single file, 4.9 MB, works but bigger than `7za` |
| `7z` (Linux) | no | the full CLI loads its codecs from `7z.so`; without it, it fails |
| `7zr` | no | the table in its docs mentions Zstd, but it cannot decode a *raw* zstd stream - only codecs inside 7z archives |

Nothing else from the distribution is copied: `7zFM.exe`/`7zG.exe` (GUI), `7z.sfx`/`7zCon.sfx`,
`7-zip.chm`, `Lang/` (90 translations, ~700 KB), `7z.so` (5 MB, only needed by the `7z` CLI),
`Uninstall.exe`, `TotalCmd`.

## macOS

Upstream **publishes no macOS build** - the mcmilk releases carry Windows (x86/x64/arm64) and
Linux (x64/arm64) only, so there is no Mach-O binary to bundle here. Three ways to make
compressed files work on a macOS Worker, in the order `blend_names.py` tries them:

1. drop a zstd-capable binary in this folder as **`7za-macos-arm64`** (Apple silicon) or
   **`7za-macos-x64`** (Intel), `chmod +x` - the code picks it up automatically, and
   `blend_names.py` re-applies the executable bit itself on every run;
2. install the `zstd` command (`brew install zstd`) - the code also tries
   `zstd -d -c` for any file the bundled binary cannot handle;
3. do nothing: it falls back to starting Blender, which does load the file (slow, and the only
   path that loads the scene).

`scan` the farm for which of these is present with
`deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\report_paths.py"`.

## Provenance and licence

* **7-Zip 26.02 ZS v1.5.7 R1**, the ZStandard fork by Tino Reichardt / Sergey G. Brester, built
  on Igor Pavlov's 7-Zip: <https://github.com/mcmilk/7-Zip-zstd>.
* Licence: **GNU LGPL-2.1-or-later**, with the usual 7-Zip restrictions (unRAR is restricted;
  the AES code is BSD). `License.txt` in this folder is the upstream licence file, unmodified.
  These binaries are merely **aggregated** with the repository, not linked into Deadline, so
  the repository keeps its own terms; keep `License.txt` beside them.
* The binaries are byte-identical copies; nothing was patched or rebuilt.

## How it is used

One command, with the output written **straight into a file handle**:

```
7za x -so -y "<file.blend>"      # decompressed bytes to stdout
```

Never let those bytes pass through a text pipe. PowerShell's `>` decodes and re-encodes the
stream, replaces every non-UTF-8 byte with U+FFFD and silently produces a corrupt file: while
testing this, the output "grew" from 8.8 MB to 12.9 MB and every block header was misaligned.
That failure mode cost an hour and is the reason the code writes to a file object instead.

`.blend` files are recognised **by signature** (`28 B5 2F FD`), so the `.blend` extension is
no obstacle - no `-tzstd` needed.

## Verified

| What | Where | Result |
| --- | --- | --- |
| 7-Zip output vs Python `compression.zstd` | Windows x64, `7za.exe` | byte-identical |
| same | Linux x86_64 under WSL, `7za` | byte-identical |
| whole reader path (chmod, subprocess, parser) on Deadline's Python | 3.10.18, `7za.exe` | `source='parser (decompressed with 7-Zip)'`, names identical to Blender |

Re-run it with:

```
<repo>\.venv\Scripts\python.exe "<DeadlineRepository>\custom\tests\test_blend_reader.py"
deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\test_blend_reader.py"
```

## Updating

Download a newer 7-Zip-zstd release, copy `7za.exe` and the Linux `7za` (as
`7za-linux-x64`) over these, refresh `License.txt`/`readme.txt`/`History.txt`, update the
sizes above, and re-run both test commands.
