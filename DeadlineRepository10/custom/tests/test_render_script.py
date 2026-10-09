"""End-to-end test for BlenderRenderScript.py against a locally installed Blender.

It runs the exact command line shape that
``custom/plugins/Blender/Blender.py`` builds, so it covers the parts that the Deadline
Worker cannot be asked about interactively:

  * the options after "--" actually reach the script (this is the transport that replaced
    the DLB_* environment variables, which did not arrive on the farm);
  * the render is triggered by the script and honours the options (engine, resolution,
    output path, frame range);
  * a task without options fails loudly instead of exiting successfully without rendering.

Usage:

    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_render_script.py
    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_render_script.py "C:\\path\\to\\blender.exe"

Without an argument every Blender found in "C:\\Program Files\\Blender Foundation" (Windows)
or on PATH is tested.
"""

import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "Blender", "BlenderRenderScript.py")
SCRIPT = os.path.abspath(SCRIPT)

MARKER = "[deadline-blender-custom]"


def find_blenders():
    candidates = []
    if os.name == "nt":
        for root in glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"):
            candidates.append(root)
    for name in ("blender", "blender.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return sorted(set(candidates))


def png_size(path):
    with open(path, "rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("%s is not a PNG" % path)
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def run_blender(blender, args, timeout=600):
    process = subprocess.run(
        [blender] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return process.returncode, process.stdout or ""


def make_test_scene(blender, blend_path):
    """Save a tiny scene whose engine (Cycles) is clearly different from the one we ask for."""
    expression = (
        "import bpy; s=bpy.context.scene;"
        "s.render.engine='CYCLES';"
        "s.render.resolution_x=64; s.render.resolution_y=64; s.render.resolution_percentage=100;"
        "s.frame_start=1; s.frame_end=1;"
        "bpy.ops.wm.save_as_mainfile(filepath=r'%s')" % blend_path
    )
    code, output = run_blender(blender, ["-b", "--factory-startup", "--python-exit-code", "1", "--python-expr", expression])
    if code != 0 or not os.path.isfile(blend_path):
        raise AssertionError("could not create the test scene:\n%s" % output)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_happy_path(blender, work_dir):
    """engine/resolution/output/frames options must all take effect."""
    blend_path = os.path.join(work_dir, "scene.blend")
    make_test_scene(blender, blend_path)

    out_dir = os.path.join(work_dir, "render")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    # Deliberately NOT created: the submission dialog's output presets write into a sub folder
    # per scene / view layer, and Blender itself does not create the directory it writes into.
    nested_dir = os.path.join(out_dir, "Scene_Main_Beauty")
    output_pattern = os.path.join(nested_dir, "Scene_Main_Beauty_ShotCam_0_####")

    args = [
        "-b", blend_path,
        "-t", "4",
        "--python-exit-code", "1",
        "--python", SCRIPT,
        "--",
        "engine=workbench",
        "resx=64", "resy=64",
        "frames=1-2",
        "threads=2",
        "output=%s" % output_pattern,
        "render=1",
    ]
    code, output = run_blender(blender, args)

    check(code == 0, "expected exit code 0, got %s\n%s" % (code, output))
    check(MARKER + " Rendering: engine=BLENDER_WORKBENCH" in output,
          "the render did not use the requested engine. Log:\n%s" % output)
    check("output=%s" % output_pattern in output, "the output option was not applied. Log:\n%s" % output)
    check("Created the output directory %s" % nested_dir in output,
          "the missing output directory was not created. Log:\n%s" % output)

    for frame in (1, 2):
        path = os.path.join(nested_dir, "Scene_Main_Beauty_ShotCam_0_%04d.png" % frame)
        check(os.path.isfile(path), "frame %s was not written: %s" % (frame, path))
        check(png_size(path) == (64, 64), "unexpected resolution for %s: %s" % (path, png_size(path)))

    print("    happy path: engine override, 2 frames, 64x64, new sub folder created  -> OK")


def test_missing_options(blender, work_dir):
    """Without the tokens the script must fail instead of exiting quietly."""
    blend_path = os.path.join(work_dir, "scene.blend")
    args = ["-b", blend_path, "--python-exit-code", "1", "--python", SCRIPT]
    code, output = run_blender(blender, args)

    check(code != 0, "expected a non-zero exit code when no options are passed, got 0\n%s" % output)
    check("Refusing to finish without rendering" in output,
          "expected the explicit failure message. Log:\n%s" % output)
    print("    no options passed: task fails with an explicit message            -> OK")


def test_bad_scene(blender, work_dir):
    """A scene that does not exist must fail the task."""
    blend_path = os.path.join(work_dir, "scene.blend")
    args = [
        "-b", blend_path,
        "--python-exit-code", "1",
        "--python", SCRIPT,
        "--",
        "scene=DoesNotExist",
        "render=1",
    ]
    code, output = run_blender(blender, args)

    check(code != 0, "expected a non-zero exit code for a missing scene, got 0\n%s" % output)
    check("does not exist in this .blend file" in output, "expected the scene error. Log:\n%s" % output)
    print("    missing scene: task fails with the scene name                     -> OK")


def test_option_value_with_spaces(blender, work_dir):
    """Quoted values (paths with spaces) must survive the command line."""
    blend_path = os.path.join(work_dir, "scene.blend")
    spaced_dir = os.path.join(work_dir, "out dir with spaces")
    if os.path.isdir(spaced_dir):
        shutil.rmtree(spaced_dir)
    os.makedirs(spaced_dir)

    args = [
        "-b", blend_path,
        "--python-exit-code", "1",
        "--python", SCRIPT,
        "--",
        "engine=workbench",
        "resx=32", "resy=32",
        "frames=1-1",
        # No manual quoting here: subprocess quotes the element when it builds the command
        # line, which is exactly what the plugin's QuoteOptionToken() produces for the OS.
        "output=%s" % os.path.join(spaced_dir, "sp_####"),
        "render=1",
    ]

    code, output = run_blender(blender, args)
    check(code == 0, "expected exit code 0, got %s\n%s" % (code, output))

    written = glob.glob(os.path.join(spaced_dir, "sp_*.png"))
    check(len(written) == 1, "expected one frame in the spaced directory, found %s" % written)
    check(png_size(written[0]) == (32, 32), "unexpected resolution: %s" % written[0])
    print("    output directory with spaces: rendered correctly                  -> OK")


def test_engine_id_mapping(blender, work_dir):
    """EEVEE is called BLENDER_EEVEE_NEXT in 4.2-4.x and BLENDER_EEVEE again from 5.0.

    Run with render=0 so the engine id is checked without needing a GPU for a real EEVEE
    render.
    """
    code, version_output = run_blender(blender, ["--version"])
    match = re.search(r"Blender ([0-9]+)\.([0-9]+)", version_output or "")
    check(match is not None, "could not read the Blender version from:\n%s" % version_output)
    version = (int(match.group(1)), int(match.group(2)))

    if (4, 2) <= version < (5, 0):
        expected = "BLENDER_EEVEE_NEXT"
    else:
        expected = "BLENDER_EEVEE"

    args = [
        "-b", os.path.join(work_dir, "scene.blend"),
        "--python-exit-code", "1",
        "--python", SCRIPT,
        "--",
        "engine=eevee",
        "render=0",
    ]
    code, output = run_blender(blender, args)

    check(code == 0, "expected exit code 0 with render=0, got %s\n%s" % (code, output))
    check("Applied: engine=%s" % expected in output,
          "expected engine id %s for Blender %s. Log:\n%s" % (expected, version, output))
    print("    engine id for Blender %s: %s -> OK" % (".".join(str(p) for p in version), expected))


def main():
    blenders = [sys.argv[1]] if len(sys.argv) > 1 else find_blenders()
    if not blenders:
        print("No Blender found; pass the executable path as the first argument.")
        return 2

    if not os.path.isfile(SCRIPT):
        print("Render script not found: %s" % SCRIPT)
        return 2

    failures = 0
    for blender in blenders:
        print("Testing %s" % blender)
        work_dir = tempfile.mkdtemp(prefix="dlb_blender_test_")
        try:
            for test in (test_happy_path, test_missing_options, test_bad_scene, test_option_value_with_spaces, test_engine_id_mapping):
                try:
                    test(blender, work_dir)
                except AssertionError as error:
                    failures += 1
                    print("    FAILED: %s" % error)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    print("")
    if failures:
        print("RESULT: %s check(s) failed" % failures)
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
