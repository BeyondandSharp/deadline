"""Read the Scene / View Layer / Camera names out of a .blend file.

Repository:  <DeadlineRepository>/custom/lib/blend_names.py

Two ways of reading, in order of preference:

1. **Parser (default).** The vendored Blender Asset Tracer parser in
   ``custom/lib/blender_asset_tracer`` reads the file's block index and its embedded SDNA
   structure catalog. It never starts Blender and never builds a single datablock, so a
   15 MB file is answered in ~0.015 s and nothing but a few hundred KB is read. This is
   the whole point of this module: no meshes, no textures, no libraries, no memory blow-up,
   and no reading of a multi-GB file across the network share.

2. **ZStandard-compressed `.blend` (the Blender 5.2 default).** ``use_file_compression`` is
   on by default in 5.2 (off in 4.5 and earlier), and a compressed stream cannot be seeked, so
   the file is decompressed to a temporary file first and then parsed - still without building
   any scene data. Decompressors are tried in order, first that works wins:

   a. the ``zstandard`` Python package, if it is importable;
   b. ``compression.zstd``, which exists from Python 3.14 on;
   c. the **bundled 7-Zip-zstd** in ``custom/lib/7zip``: ``7z.exe`` + ``7z.dll`` on Windows,
      ``7zz-linux-x64`` on Linux, ``7zz-macos-x64`` / ``7zz-macos-arm64`` on macOS (upstream
      publishes no macOS build, so those two are optional - see ``lib/7zip/README.md``). No
      installation, and independent of which Python runs it, which is what makes this work on
      Deadline's Windows Python 3.10 where neither of the other two exists;
   d. a ``zstd`` command on PATH (``brew install zstd`` on macOS, ``apt install zstd`` on
      Linux) - the fallback for a platform whose bundled binary is missing.

   Only when none of those is available does it fall back to starting a **Blender
   executable**, which *does* load the file - the slow path, and the only one that does.

Used by ``custom/scripts/Jobs/blender_render_options.py`` (Monitor -> right-click a job ->
Scripts). Deliberately free of Deadline imports so it can be tested on its own:

    <repo>\\.venv\\Scripts\\python.exe custom\\lib\\blend_names.py <file.blend> [blender.exe]
"""

import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time

# The 2-character ID code that Blender puts in front of an ID name inside a .blend file,
# e.g. an object named "Cube" is stored as "OBCube".
ID_CODE_LENGTH = 2

# Object.type == OB_CAMERA in Blender's DNA. Cross-checked against the data pointer (the
# referenced block must be a Camera) so a wrong constant cannot silently produce nonsense.
OB_CAMERA = 11

BLENDER_PROBE = r'''
import bpy, json
print("DLB_NAMES_JSON " + json.dumps({
    "scenes": {scene.name: sorted(layer.name for layer in scene.view_layers)
               for scene in bpy.data.scenes},
    "cameras": sorted(obj.name for obj in bpy.data.objects if obj.type == "CAMERA"),
}))
'''


class BlendNamesError(Exception):
    """Raised when the names cannot be read."""


class CompressedBlendError(BlendNamesError):
    """Raised when the file is compressed and no decompressor is available."""


# The vendored parser, as repository-relative paths.
#
# Deadline's Repository Cache Service fetches files on demand: it caches a file when it is
# asked for it, and knows nothing about files that only Python would import. A script running
# from the cache therefore has to request every one of these through
# RepositoryUtils.GetRepositoryFilePath() before importing this module's parser - that is what
# callers should iterate over. Kept here so the list lives next to the package it describes.
VENDOR_FILES = (
    "lib/blender_asset_tracer/__init__.py",
    "lib/blender_asset_tracer/bpathlib.py",
    "lib/blender_asset_tracer/cdefs.py",
    "lib/blender_asset_tracer/blendfile/__init__.py",
    "lib/blender_asset_tracer/blendfile/dna.py",
    "lib/blender_asset_tracer/blendfile/dna_io.py",
    "lib/blender_asset_tracer/blendfile/exceptions.py",
    "lib/blender_asset_tracer/blendfile/header.py",
    "lib/blender_asset_tracer/blendfile/iterators.py",
    "lib/blender_asset_tracer/blendfile/magic_compression.py",
)

# The bundled 7-Zip, per platform, as repository-relative paths. Only this platform's files
# are requested from the repository (see repository_files), so a Windows client does not pull
# the Linux binary and vice versa.
#
# `7za` is used because it is a single self-contained file with the zstd codec, on both
# platforms - measured against this bundle:
#
#   7za.exe / 7za (Linux)   zstd present, one file, 1.8 MB / 2.6 MB   <- used
#   7z.exe + 7z.dll (Win)   zstd present, two files, 5.7 MB
#   7z (Linux)              needs 7z.so next to it, and fails without it
#   7zr                     no zstd for a raw stream (only inside 7z archives)
#
# The second name is a fallback: a hand-dropped `7z.exe` only works with its `7z.dll`, so the
# candidates are tried in order and the first one that really decompresses wins.
#
# Upstream (mcmilk/7-Zip-zstd) publishes Windows and Linux builds only - for macOS, either
# drop a zstd-capable binary in here as 7za-macos-x64 / 7zz-macos-arm64 (it is then picked up
# automatically), or install the `zstd` command (brew install zstd), or let it fall back.
# The second group is only used for *finding* a binary that somebody dropped in by hand; it is
# deliberately not part of repository_files(), because a file that is not shipped should not be
# requested from the repository on every machine.
EXTRA_SEVENZIP_NAMES = ("7za", "7zz", "7z", "7z.exe")

SEVENZIP_FILES_BY_PLATFORM = {
    "windows": ("7za.exe",),
    "linux-x64": ("7za-linux-x64",),
    "linux-arm64": ("7za-linux-arm64",),
    "macos-x64": ("7za-macos-x64",),
    "macos-arm64": ("7za-macos-arm64",),
}


def platform_key():
    """One of the SEVENZIP_FILES_BY_PLATFORM keys, for the machine running this code."""
    machine = platform.machine().lower()
    isArm = machine in ("arm64", "aarch64")

    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos-arm64" if isArm else "macos-x64"
    return "linux-arm64" if isArm else "linux-x64"


def sevenzip_filenames():
    """Names to look for in lib/7zip, this platform's first, then a hand-dropped binary."""
    names = list(SEVENZIP_FILES_BY_PLATFORM.get(platform_key(), ()))
    for name in EXTRA_SEVENZIP_NAMES:
        if name not in names:
            names.append(name)
    return names


def repository_files():
    """Repository-relative paths this machine needs, for RepositoryUtils.GetRepositoryFilePath.

    Deadline's Repository Cache Service fetches a file only when it is asked for it, so a
    caller that wants to import this module and use its parser has to request every file
    listed here first.
    """
    return VENDOR_FILES + tuple("lib/7zip/" + name
                                for name in SEVENZIP_FILES_BY_PLATFORM.get(platform_key(), ()))


def vendor_root():
    """Directory that has to be on sys.path for the vendored parser."""
    return os.path.dirname(os.path.abspath(__file__))


def _ensure_vendor_on_path():
    root = vendor_root()
    if root not in sys.path:
        sys.path.insert(0, root)


def _text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).split("\x00")[0]


def _id_name(block):
    """ID name of a block, without the 2-character ID code ('SCScene' -> 'Scene')."""
    return _text(block.id_name)[ID_CODE_LENGTH:]


# --------------------------------------------------------------------------------------
# 1. The parser
# --------------------------------------------------------------------------------------

def _read_with_parser(blend_path):
    _ensure_vendor_on_path()
    from blender_asset_tracer import blendfile
    from blender_asset_tracer.blendfile import iterators

    bfile = blendfile.BlendFile(pathlib.Path(blend_path))
    try:
        scenes = {}
        for block in bfile.find_blocks_from_code(b"SC"):
            first_layer = block.get_pointer((b"view_layers", b"first"))
            # Sorted so the result does not depend on the order the linked list happens to use.
            scenes[_id_name(block)] = sorted(
                _text(layer.get(b"name")) for layer in iterators.listbase(first_layer)
            )

        cameras = []
        for block in bfile.blocks:
            if block.dna_type_name != "Object" or block.get(b"type") != OB_CAMERA:
                continue
            data_block = block.get_pointer(b"data")
            if data_block is None or data_block.dna_type_name != "Camera":
                continue
            cameras.append(_id_name(block))

        return {"scenes": scenes, "cameras": sorted(cameras)}
    finally:
        bfile.close()


def _decompress_zstd(source, target):
    """Decompress a ZStandard .blend with a Python module. False when none is available."""
    try:
        import zstandard  # type: ignore
    except ImportError:
        zstandard = None

    if zstandard is not None:
        with open(source, "rb") as src, open(target, "wb") as dst:
            # Blender writes one zstd frame per block region (19 frames in a test scene), so
            # the frames have to be read across, not just the first one.
            reader = zstandard.ZstdDecompressor().stream_reader(src, read_across_frames=True)
            try:
                shutil.copyfileobj(reader, dst, 1 << 20)
            finally:
                reader.close()
        return True

    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        return False

    with open(source, "rb") as src, open(target, "wb") as dst:
        with zstd.ZstdFile(src) as reader:
            shutil.copyfileobj(reader, dst, 1 << 20)
    return True


def sevenzip_executables():
    """Every bundled 7-Zip candidate that exists on this machine, best first."""
    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "7zip")
    found = []

    for name in sevenzip_filenames():
        candidate = os.path.join(directory, name)
        if not os.path.isfile(candidate):
            continue

        if os.name != "nt":
            # The repository cache and most sync methods do not preserve the executable bit,
            # and 7-Zip is useless without it.
            try:
                os.chmod(candidate, 0o755)
            except OSError:
                pass
        found.append(candidate)

    return found


def sevenzip_executable():
    """This platform's bundled 7-Zip, if it is next to this file (see lib/7zip/README.md)."""
    executables = sevenzip_executables()
    return executables[0] if executables else ""


def _decompress_with_7zip(source, target, timeout=3600):
    """Decompress with a bundled 7-Zip. False when there is none.

    This is what makes compressed files work on Deadline's own Python 3.10, which has neither
    the `zstandard` module nor `compression.zstd`. 7-Zip decodes Blender's multi-frame stream
    correctly (verified byte for byte against Python's zstd on Windows and on Linux) and does
    not care which Python version runs it.

    Candidates are tried in order, because a bundled `7z.exe` is useless without its `7z.dll`:
    the first one that really produces a file wins.

    The output goes **straight into a file handle**: letting the bytes pass through a text pipe
    (PowerShell's ``>``) decodes and re-encodes them, replaces every non-UTF-8 byte with U+FFFD
    and silently produces a corrupt file.
    """
    executables = sevenzip_executables()
    if not executables:
        return False

    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flash
    failures = []

    for executable in executables:
        with open(target, "wb") as output:
            process = subprocess.run(
                [executable, "x", "-so", "-y", str(source)],
                stdout=output, stderr=subprocess.PIPE, timeout=timeout, creationflags=creationflags,
            )
        if process.returncode == 0:
            return True
        failures.append("%s exited with %s: %s"
                        % (os.path.basename(executable), process.returncode,
                           _text(process.stderr)[:200]))

    raise BlendNamesError("no bundled 7-Zip could decompress %s:\n  %s"
                          % (source, "\n  ".join(failures)))


def _decompress_with_zstd_command(source, target, timeout=3600):
    """Decompress with a `zstd` command found on PATH. False when there is none.

    This is the macOS route: upstream 7-Zip-zstd publishes no macOS build, but
    ``brew install zstd`` provides a command that decodes Blender's multi-frame stream
    correctly. It is also a valid fallback on Linux.
    """
    executable = shutil.which("zstd")
    if not executable:
        return False

    with open(target, "wb") as output:
        process = subprocess.run(
            [executable, "-d", "-c", "-q", "-f", str(source)],
            stdout=output, stderr=subprocess.PIPE, timeout=timeout,
        )

    if process.returncode != 0:
        raise BlendNamesError(
            "the zstd command could not decompress %s (exit code %s): %s"
            % (source, process.returncode, _text(process.stderr)[:500])
        )
    return True


# --------------------------------------------------------------------------------------
# 2. The Blender fallback
# --------------------------------------------------------------------------------------

def _read_with_blender(blend_path, blender_exe, timeout):
    probe_path = None
    try:
        handle, probe_path = tempfile.mkstemp(suffix=".py", prefix="dlb_names_")
        with os.fdopen(handle, "w", encoding="utf-8") as probe:
            probe.write(BLENDER_PROBE)

        process = subprocess.run(
            [blender_exe, "-b", str(blend_path), "--factory-startup",
             "--python-exit-code", "1", "--python", probe_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    finally:
        if probe_path and os.path.exists(probe_path):
            os.unlink(probe_path)

    output = process.stdout or ""
    for line in output.splitlines():
        if line.startswith("DLB_NAMES_JSON "):
            return json.loads(line[len("DLB_NAMES_JSON "):])

    raise BlendNamesError(
        "Blender (%s) did not report the scene names (exit code %s). Output tail:\n%s"
        % (blender_exe, process.returncode, "\n".join(output.splitlines()[-15:]))
    )


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------

def read_names(blend_path, blender_exe=None, timeout=600):
    """Return {"scenes": {scene: [view layers]}, "cameras": [...], "source": ..., "seconds": ...}.

    Raises BlendNamesError (or its CompressedBlendError subclass) with an actionable message
    when the file cannot be read.
    """
    blend_path = str(blend_path)
    if not os.path.isfile(blend_path):
        raise BlendNamesError("No such .blend file: %s" % blend_path)

    started = time.time()

    try:
        names = _read_with_parser(blend_path)
        names["source"] = "parser"
    except CompressedBlendError:
        raise
    except OSError as error:
        if "ZStandard" not in str(error):
            raise BlendNamesError("Could not parse %s: %s" % (blend_path, error))

        # Compressed with ZStandard: decompress to a temporary file and parse that.
        target = os.path.join(tempfile.gettempdir(), "dlb_uncompressed_%d.blend" % os.getpid())
        try:
            if _decompress_zstd(blend_path, target):
                source = "parser (decompressed with zstd)"
            elif _decompress_with_7zip(blend_path, target):
                source = "parser (decompressed with 7-Zip)"
            elif _decompress_with_zstd_command(blend_path, target):
                source = "parser (decompressed with the zstd command)"
            else:
                source = ""

            if source:
                names = _read_with_parser(target)
                names["source"] = source
            elif blender_exe:
                names = _read_with_blender(blend_path, blender_exe, timeout)
                names["source"] = "blender"
            else:
                raise CompressedBlendError(
                    "%s is ZStandard-compressed (Blender 5.2 compresses .blend files by default), and "
                    "this Python (%s) on %s has no zstd module, and no bundled 7-Zip was found "
                    "(%s), and there is no 'zstd' command on PATH.\n"
                    "Options:\n"
                    "  * restore custom/lib/7zip for this platform - see lib/7zip/README.md, which "
                    "needs no installation at all;\n"
                    "  * or install the 'zstandard' package into the Python that runs this script;\n"
                    "  * or install the 'zstd' command (on macOS: brew install zstd);\n"
                    "  * or tick 'Compress File' off in Blender: Preferences -> Save & Load, so new "
                    "files stay uncompressed;\n"
                    "  * or give this script a Blender executable, which will then open the file "
                    "(slower, and it does load the scene)."
                    % (blend_path, sys.version.split()[0], platform_key(),
                       sevenzip_executable() or "not found")
                )
        finally:
            if os.path.exists(target):
                os.unlink(target)
    except Exception as error:  # parser bugs on unusual files
        raise BlendNamesError("Could not parse %s: %s: %s" % (blend_path, type(error).__name__, error))

    names["seconds"] = round(time.time() - started, 3)
    return names


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2

    blender_exe = argv[2] if len(argv) > 2 else None
    started = time.time()
    names = read_names(argv[1], blender_exe=blender_exe)
    print("source : %s (%.3fs)" % (names["source"], names["seconds"]))
    print("cameras: %s" % ", ".join(names["cameras"]) or "<none>")
    for scene, layers in names["scenes"].items():
        print("scene  : %-20s view layers: %s" % (scene, ", ".join(layers)))
    print("total  : %.3fs" % (time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
