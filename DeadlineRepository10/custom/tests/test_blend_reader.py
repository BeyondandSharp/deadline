"""Tests for custom/lib/blend_names.py - reading names from a .blend without loading it.

Runs against a locally installed Blender (it generates its own test files), and needs no
Deadline at all:

    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_blend_reader.py
    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_blend_reader.py "<path to blender.exe>"

Checks the three read paths:
  * uncompressed .blend  -> the vendored parser (no Blender started, no scene data built)
  * ZStandard-compressed -> the parser after decompressing, or Blender when nothing can decompress
  * nothing usable       -> a CompressedBlendError with an actionable message
and that every result matches what Blender itself reports.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.normpath(os.path.join(HERE, "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import blend_names  # noqa: E402  (needs the sys.path line above)

MAKE = r'''
import bpy
bpy.ops.mesh.primitive_grid_add(x_subdivisions=400, y_subdivisions=400)
bpy.context.object.name = "HeavyGrid"
for index in range(3):
    cam_data = bpy.data.cameras.new("CamData_%d" % index)
    cam_obj = bpy.data.objects.new("ShotCam_%d" % index, cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
main = bpy.context.scene
main.name = "Scene_Main"
for name in ("Beauty", "Mask"):
    main.view_layers.new(name)
alt = bpy.data.scenes.new("Scene_Alt")
for name in ("Layers_A", "Layers_B"):
    alt.view_layers.new(name)
bpy.ops.wm.save_as_mainfile(filepath=r"__PLAIN__", compress=False)
bpy.ops.wm.save_as_mainfile(filepath=r"__ZSTD__", compress=True)
'''

TRUTH = r'''
import bpy, json
print("TRUTH " + json.dumps({
    "scenes": {s.name: sorted(l.name for l in s.view_layers) for s in bpy.data.scenes},
    "cameras": sorted(o.name for o in bpy.data.objects if o.type == "CAMERA"),
}))
'''

FAILURES = []


def check(condition, message):
    if condition:
        print("    OK   %s" % message)
    else:
        FAILURES.append(message)
        print("    FAIL %s" % message)


def scene_diff(result, truth):
    """Human readable difference between two {"scene": [layers]} dicts."""
    lines = []
    for scene in sorted(set(result) | set(truth)):
        got, want = result.get(scene), truth.get(scene)
        if got != want:
            lines.append("\n        %-16s read=%s blender=%s" % (scene, got, want))
    return "".join(lines)


def find_blenders():
    found = []
    if os.name == "nt":
        found += glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe")
    for name in ("blender", "blender.exe"):
        path = shutil.which(name)
        if path:
            found.append(path)
    return sorted(set(found))


def BlenderFromArguments():
    """A Blender executable passed on the command line (only when it really is one).

    Under ``deadlinecommand -ExecuteScript`` argv[1] is this script itself, so it must not be
    mistaken for a Blender path.
    """
    if len(sys.argv) > 1:
        candidate = sys.argv[1]
        if os.path.isfile(candidate) and os.path.basename(candidate).lower().startswith("blender"):
            return candidate
    return ""


def run(blender, args, timeout=900):
    process = subprocess.run(
        [blender] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    return process.stdout or ""


def __main__():
    given = BlenderFromArguments()
    blenders = [given] if given else find_blenders()
    if not blenders:
        print("No Blender found; pass the executable path as the first argument.")
        return 2
    blender = blenders[-1]
    print("Blender : %s" % blender)

    try:
        import zstandard  # noqa: F401
        print("zstd    : zstandard package available")
    except ImportError:
        try:
            from compression import zstd  # noqa: F401
            print("zstd    : compression.zstd available (Python %s)" % sys.version.split()[0])
        except ImportError:
            print("zstd    : no decompressor available (the Blender fallback will be used)")

    work = tempfile.mkdtemp(prefix="dlb_names_test_")
    plain = os.path.join(work, "plain.blend")
    compressed = os.path.join(work, "compressed.blend")
    try:
        script = os.path.join(work, "make.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(MAKE.replace("__PLAIN__", plain).replace("__ZSTD__", compressed))
        run(blender, ["-b", "--factory-startup", "--python-exit-code", "1", "--python", script])
        if not (os.path.isfile(plain) and os.path.isfile(compressed)):
            print("could not create the test files")
            return 1

        truth_script = os.path.join(work, "truth.py")
        with open(truth_script, "w", encoding="utf-8") as handle:
            handle.write(TRUTH)
        match = re.search(r"TRUTH (\{.*\})", run(blender, ["-b", plain, "--python-exit-code", "1", "--python", truth_script]))
        if not match:
            print("could not read the ground truth from Blender")
            return 1
        truth = json.loads(match.group(1))

        print("")
        print("uncompressed .blend (%s)" % os.path.basename(plain))
        result = blend_names.read_names(plain)
        check(result["source"] == "parser", "read by the vendored parser (source=%r)" % result["source"])
        check(result["scenes"] == truth["scenes"], "per-scene view layers match Blender" + scene_diff(result["scenes"], truth["scenes"]))
        check(result["cameras"] == truth["cameras"], "camera objects match Blender")
        check(result["seconds"] < 1.0, "answered in %.3fs" % result["seconds"])

        print("")
        print("ZStandard-compressed .blend (%s, %.1f MB -> %.1f MB)"
              % (os.path.basename(compressed), os.path.getsize(plain) / 1048576.0,
                 os.path.getsize(compressed) / 1048576.0))
        check(blend_names.sevenzip_executable() != "",
              "the bundled 7-Zip is present: %s" % blend_names.sevenzip_executable())
        try:
            result = blend_names.read_names(compressed, blender_exe=blender)
            check("parser" in result["source"], "read without loading the scene (source=%r)" % result["source"])
            check(result["scenes"] == truth["scenes"], "per-scene view layers match Blender" + scene_diff(result["scenes"], truth["scenes"]))
            check(result["cameras"] == truth["cameras"], "camera objects match Blender")
        except blend_names.CompressedBlendError:
            print("    (no decompressor in this Python, checking the Blender fallback instead)")

        print("")
        print("the bundled 7-Zip alone (no zstd module at all)")
        original = blend_names._decompress_zstd
        blend_names._decompress_zstd = lambda source, target: False
        try:
            result = blend_names.read_names(compressed)
            check("7-Zip" in result["source"],
                  "decompressed with the bundled 7-Zip (source=%r)" % result["source"])
            check(result["scenes"] == truth["scenes"], "per-scene view layers match Blender" + scene_diff(result["scenes"], truth["scenes"]))
            check(result["cameras"] == truth["cameras"], "camera objects match Blender")
        finally:
            blend_names._decompress_zstd = original

        print("")
        print("no decompressor available")
        original = blend_names._decompress_zstd
        blend_names._decompress_zstd = lambda source, target: False
        original7z = blend_names._decompress_with_7zip
        blend_names._decompress_with_7zip = lambda source, target: False
        try:
            try:
                blend_names.read_names(compressed)
                check(False, "expected CompressedBlendError without a decompressor and without Blender")
            except blend_names.CompressedBlendError as error:
                check("Compress File" in str(error), "CompressedBlendError explains how to fix it")
                check("lib/7zip" in str(error), "it mentions the bundled 7-Zip")

            result = blend_names.read_names(compressed, blender_exe=blender)
            check(result["source"] == "blender", "fell back to Blender (source=%r)" % result["source"])
            check(result["scenes"] == truth["scenes"], "fallback results match Blender")
            check(result["cameras"] == truth["cameras"], "fallback cameras match Blender")
        finally:
            blend_names._decompress_zstd = original
            blend_names._decompress_with_7zip = original7z

        print("")
        print("missing file")
        try:
            blend_names.read_names(os.path.join(work, "nope.blend"))
            check(False, "expected an error for a missing file")
        except blend_names.BlendNamesError as error:
            check("No such .blend file" in str(error), "clear error for a missing file")

        print("")
        print("platform selection (which bundled 7-Zip is looked for)")
        realPlatform = sys.platform
        realMachine = blend_names.platform.machine
        try:
            for platformName, machine, expectedKey, expectedBinary in (
                ("win32", "AMD64", "windows", "7za.exe"),
                ("linux", "x86_64", "linux-x64", "7za-linux-x64"),
                ("linux", "aarch64", "linux-arm64", "7za-linux-arm64"),
                ("darwin", "arm64", "macos-arm64", "7za-macos-arm64"),
                ("darwin", "x86_64", "macos-x64", "7za-macos-x64"),
            ):
                sys.platform = platformName
                blend_names.platform.machine = lambda value=machine: value
                key = blend_names.platform_key()
                check(key == expectedKey, "%-7s %-8s -> %s" % (platformName, machine, key))
                check(blend_names.sevenzip_filenames()[0] == expectedBinary,
                      "%-7s %-8s looks for %s first" % (platformName, machine, expectedBinary))
        finally:
            sys.platform = realPlatform
            blend_names.platform.machine = realMachine
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("")
    if FAILURES:
        print("RESULT: %d check(s) failed" % len(FAILURES))
        for failure in FAILURES:
            print("  - %s" % failure)
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(__main__())
