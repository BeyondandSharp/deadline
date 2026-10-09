"""Build the Blender-side submission context in a real Blender and check what it carries.

This is the data the submission dialog's render tree is filled from, so it has to contain
**every scene** with **its own** view layers and cameras - a scene that is not on screen
included. That is what used to be missing.

    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_submit_context.py [blender.exe]
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
CUSTOM = os.path.normpath(os.path.join(HERE, ".."))
PROXY_DIRECTORY = os.path.join(CUSTOM, "submission", "Blender", "Main")

MAKE = r'''
import bpy

# Start from nothing, so the names are exactly the ones this test created.
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
for scene in list(bpy.data.scenes)[1:]:
    bpy.data.scenes.remove(scene)

# Scene 1: two cameras, two view layers.
main = bpy.context.scene
main.name = "Scene_Main"
main.view_layers.new("Beauty")
for index in range(2):
    camera = bpy.data.objects.new("ShotCam_%d" % index, bpy.data.cameras.new("CamData_%d" % index))
    main.collection.objects.link(camera)
main.camera = bpy.data.objects["ShotCam_0"]
main.render.resolution_x = 1920
main.render.resolution_y = 1080

# Scene 2: its own camera and view layer, never the active scene. A new scene always comes
# with one default view layer ("ViewLayer"), which the test accounts for.
alt = bpy.data.scenes.new("Scene_Alt")
alt.view_layers.new("Layers_A")
altCamera = bpy.data.objects.new("AltCam", bpy.data.cameras.new("AltCamData"))
alt.collection.objects.link(altCamera)
alt.camera = altCamera
alt.render.resolution_x = 1280
alt.render.resolution_y = 720

bpy.ops.wm.save_as_mainfile(filepath=r"__BLEND__", compress=False)
print("MADE " + bpy.context.scene.name)
'''

PROBE = r'''
import bpy, json, sys
sys.path.insert(0, r"__PROXY__")
import SubmitBlenderToDeadline as proxy
print("CTX " + json.dumps(proxy.BuildSubmitContext(bpy.context.scene)))
'''

FAILURES = []


def check(condition, message):
    if condition:
        print("    OK   %s" % message)
    else:
        FAILURES.append(message)
        print("    FAIL %s" % message)


def find_blenders():
    found = []
    if os.name == "nt":
        found += glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe")
    for name in ("blender", "blender.exe"):
        path = shutil.which(name)
        if path:
            found.append(path)
    return sorted(set(found))


def run(blender, args, timeout=600):
    process = subprocess.run([blender] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, encoding="utf-8", errors="replace",
                             timeout=timeout)
    return process.stdout or ""


def __main__():
    blenders = [sys.argv[1]] if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) else find_blenders()
    if not blenders:
        print("No Blender found; pass the executable path as the first argument.")
        return 2
    blender = blenders[-1]
    print("Blender : %s" % blender)
    print("proxy   : %s" % os.path.join(PROXY_DIRECTORY, "SubmitBlenderToDeadline.py"))

    work = tempfile.mkdtemp(prefix="dlb_context_test_")
    try:
        blend = os.path.join(work, "two_scenes.blend")

        makeScript = os.path.join(work, "make.py")
        with open(makeScript, "w", encoding="utf-8") as handle:
            handle.write(MAKE.replace("__BLEND__", blend))
        run(blender, ["-b", "--factory-startup", "--python-exit-code", "1", "--python", makeScript])
        if not os.path.isfile(blend):
            print("could not create the test file")
            return 1

        probeScript = os.path.join(work, "probe.py")
        with open(probeScript, "w", encoding="utf-8") as handle:
            handle.write(PROBE.replace("__PROXY__", PROXY_DIRECTORY))
        output = run(blender, ["-b", blend, "--python-exit-code", "1", "--python", probeScript])

        match = re.search(r"CTX (\{.*\})", output)
        if not match:
            print("the proxy did not report a context. Output tail:")
            print("\n".join(output.splitlines()[-15:]))
            return 1

        context = json.loads(match.group(1))

        print("")
        print("the context of a two-scene file")
        details = context.get("scene_details") or {}
        check(sorted(details.keys()) == ["Scene_Alt", "Scene_Main"],
              "every scene is reported: %s" % sorted(details.keys()))
        check(context.get("active_scene") == "Scene_Main",
              "the scene that is being submitted is named (%r)" % context.get("active_scene"))

        print("")
        print("the active scene's own names")
        main = details.get("Scene_Main") or {}
        check("ViewLayer" in (main.get("view_layers") or []) and "Beauty" in (main.get("view_layers") or []),
              "its view layers: %s" % main.get("view_layers"))
        check(sorted(main.get("cameras") or []) == ["ShotCam_0", "ShotCam_1"],
              "its cameras: %s" % main.get("cameras"))
        check(context.get("active_camera") == "ShotCam_0",
              "the active camera is reported (%r)" % context.get("active_camera"))
        check(context.get("active_view_layer") in (main.get("view_layers") or []),
              "the active view layer is reported and exists (%r)" % context.get("active_view_layer"))

        print("")
        print("the scene that is NOT on screen")
        alt = details.get("Scene_Alt") or {}
        check("Layers_A" in (alt.get("view_layers") or []),
              "its view layers are readable: %s" % alt.get("view_layers"))
        check(sorted(alt.get("view_layers") or []) != sorted(main.get("view_layers") or []),
              "and are its own, not the active scene's: %s" % alt.get("view_layers"))
        check(alt.get("cameras") == ["AltCam"], "its cameras are readable: %s" % alt.get("cameras"))
        check(context.get("view_layers") != alt.get("view_layers"),
              "and are not confused with the active scene's (flat list: %s)" % context.get("view_layers"))

        print("")
        print("the resolution of every scene (the options table uses it as the default)")
        check((main.get("resolution_x"), main.get("resolution_y")) == (1920, 1080),
              "the active scene: %s x %s" % (main.get("resolution_x"), main.get("resolution_y")))
        check((alt.get("resolution_x"), alt.get("resolution_y")) == (1280, 720),
              "the scene that is not on screen: %s x %s" % (alt.get("resolution_x"), alt.get("resolution_y")))

        print("")
        if FAILURES:
            print("RESULT: %d check(s) failed" % len(FAILURES))
            for failure in FAILURES:
                print("  - %s" % failure)
            return 1
        print("RESULT: every scene's view layers and cameras reach the dialog")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(__main__())
