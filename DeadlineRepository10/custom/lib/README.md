# `custom/lib` - shared code for the Blender customisations

Two things live here:

| Path | Origin | Licence |
| --- | --- | --- |
| `blender_asset_tracer/` | **Vendored** third-party code, see below | GPL-2.0-or-later |
| `7zip/` | **Vendored** 7-Zip-zstd binaries (`7z.exe` + `7z.dll`), see `7zip/README.md` | LGPL-2.1-or-later |
| `blend_names.py` | Ours (written for this repository) | same as the rest of `custom/` |
| `LICENSE.txt` | Licence text shipped with the vendored package | GPL-2.0-or-later |

## Why this folder exists

`blend_names.py` needs to answer "which Scenes, View Layers and Cameras does this .blend
file contain?" **without loading the file**. Blender itself cannot do that: every way of
asking it (`bpy.data.libraries.load` for the datablock index, or opening the file) either
loses the view layers, reports camera *data block* names instead of camera *object* names,
or builds the whole scene in memory.

The vendored parser reads the file's block index and its embedded SDNA structure catalog,
so it answers in ~0.015 s for a 15 MB file, reads a few hundred KB instead of the whole
file (important when the .blend lives on a network share), and never touches Blender.

Compressed `.blend` files (Blender 5.2 saves them ZStandard-compressed by default) need
decompressing first, because such a stream cannot be seeked. `7zip/` makes that work with no
installation at all - see `7zip/README.md`.

It must **not** be placed in a Deadline script folder (`scripts/Jobs`, `scripts/General`,
...) - everything there shows up as a menu entry in the Monitor.

## Vendored package: Blender Asset Tracer

* Upstream: <https://projects.blender.org/blender/blender-asset-tracer> ("Blender Asset
  Tracer", by Sybren A. Stüvel / Blender Foundation), PyPI package `blender-asset-tracer`.
* **Version vendored: 1.20** (from the PyPI wheel `blender_asset_tracer-1.20-py3-none-any.whl`).
* Licence: **GPL-2.0-or-later** (the same licence family as the Deadline Blender plugin
  this repository customises), see `LICENSE.txt` and the GPL header block at the top of
  every vendored file. The files are included **unmodified**, so they must be kept
  byte-identical apart from this note.
* Files taken (9 files, ~76 KB):

  ```
  blender_asset_tracer/__init__.py
  blender_asset_tracer/bpathlib.py
  blender_asset_tracer/cdefs.py
  blender_asset_tracer/blendfile/__init__.py
  blender_asset_tracer/blendfile/dna.py
  blender_asset_tracer/blendfile/dna_io.py
  blender_asset_tracer/blendfile/exceptions.py
  blender_asset_tracer/blendfile/header.py
  blender_asset_tracer/blendfile/iterators.py
  blender_asset_tracer/blendfile/magic_compression.py
  ```

  (the `blendfile/` package imports `from blender_asset_tracer import bpathlib` and
  `from blender_asset_tracer import cdefs`, which is why the package name and the two
  sibling modules must be kept as they are)
* Not vendored: the `cli/`, `pack/`, `trace/` subpackages and their dependencies
  (`requests`, `cattrs`, `boto3`) - the parser does not need any of them.

### Why 1.20 and not the current 2.x

* BAT **2.x moved dependency discovery into Blender**: its `bat` CLI entry point is
  `venv_support.loop_via_blender(...)`, i.e. it re-runs itself inside a Blender
  executable found via `$BAT_BLENDER`/`PATH`, and then calls `bpy.ops.wm.open_mainfile` +
  `bpy.data.file_path_foreach`/`user_map`. That *does* load the scene, which is exactly
  what we are avoiding. It also declares `Requires-Python == 3.13.*` (the Python bundled
  with Blender 5.x), while the Monitor here runs Python 3.10/3.11.
* Inside 1.x, **1.20 is the newest release that still supports Python 3.9+**
  (1.21-1.23 require >= 3.11, and 2.x requires 3.13). All vendored files were checked to
  parse under Python 3.10 syntax rules.

### Updating the vendored copy

1. Download the wheel of the chosen 1.x release from PyPI.
2. Copy the nine files listed above over this folder, leaving them unmodified.
3. Re-run `custom/tests/test_blend_reader.py` (it compares the parser against Blender
   itself) and `deadlinecommand -ExecuteScript custom/tests/verify_custom_overrides.py`.
4. Update the version number in this file.
