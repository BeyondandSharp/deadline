#!/usr/bin/env python3
#
# Worker-side render script, loaded by Blender itself:
#
#   blender -b "<scene>" -t <threads> --python-exit-code 1 --python "<this file>" -- <options>
#
# Repository:  <DeadlineRepository>/custom/plugins/Blender/BlenderRenderScript.py
#
# It is executed by the Deadline Blender plugin (custom/plugins/Blender/Blender.py),
# applies the job's render options and then renders the task's frames.
#
# Why the render is triggered here instead of with "-s/-e/-a" on the command line:
# Blender does not process its command line strictly left to right, it handles the
# arguments in passes (ARG_PASS_* in creator_intern.h), and the render flags are handled
# in an earlier pass than "--python". A "--python" script placed before "-a" therefore
# runs after the render has already happened, so settings it applies (engine, GPU,
# resolution, ...) have no effect on the output. Rendering from the script removes the
# dependency on that ordering entirely.
#
# Why the options arrive after "--": Blender hands everything after "--" to the script in
# sys.argv, which works regardless of the passes above. The plugin also sets DLB_*
# environment variables, but they did not reach Blender on this farm, so they are only a
# fallback here.
#
# Deadline's behaviour is preserved: one render per task with the task's frame range, one
# "Saved: ..." line per frame for progress, and a non-zero exit code if anything in the
# script fails (thanks to --python-exit-code). If no options arrive at all, the script
# raises instead of exiting successfully without having rendered.
#
# Options (command line tokens after "--", or DLB_* environment variables):
#
#   engine=cycles|eevee|workbench   render engine              (absent = keep .blend value)
#   scene=<name>                    scene to render            (absent = keep)
#   layer=<name>                    view layer to render       (absent = keep all layers)
#   camera=<name>                   camera to render           (absent = keep)
#   format=PNG|OPEN_EXR|...         output image format        (absent = keep)
#   resx=<int> resy=<int>           resolution, 0 = keep
#   gpu=NONE|CUDA|OptiX|HIP|oneAPI|Metal                       (absent = keep)
#   markers=1                       also rebind timeline markers to the camera
#   frames=<start>-<end>            the frame range of this task
#   output=<path>                   output path override       (absent = keep .blend value)
#   threads=<int>                   thread count, 0 = all CPUs (absent = keep)
#   render=1                        render after applying the settings (doubles as "0" = apply only)
#
# Any problem raises, which combined with "--python-exit-code 1" fails the task with
# a readable message instead of silently rendering the wrong thing.

import os
import re
import sys

import bpy

ENV_PREFIX = "DLB_"

# Frame range tokens look like "1-10"; negative frames are accepted too.
FRAME_RANGE_PATTERN = re.compile(r"^(-?[0-9]+)-(-?[0-9]+)$")

# Cycles GPU device fallback order, matching the AWS Deadline Cloud Blender adaptor.
GPU_FALLBACK_ORDER = ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")

# Logical engine name -> Blender engine id. EEVEE was renamed in 4.2 and back in 5.0.
def _engine_candidates(logical_name):
    logical_name = (logical_name or "").strip().lower()
    if logical_name == "cycles":
        return ["CYCLES"]
    if logical_name == "workbench":
        return ["BLENDER_WORKBENCH"]
    if logical_name == "eevee":
        version = tuple(bpy.app.version)
        if (4, 2, 0) <= version < (5, 0, 0):
            return ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]
        return ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"]
    return []


def _parse_command_line_options():
    """Collect the "key=value" tokens the plugin passes after "--".

    Blender keeps everything after "--" in sys.argv for the script, which makes this the
    reliable transport: it does not depend on Blender's argument passes and it does not
    depend on the DLB_* environment variables reaching this process (they did not on this
    farm, so those are only a fallback).
    """
    argv = list(sys.argv)
    if "--" not in argv:
        return {}

    options = {}
    for token in argv[argv.index("--") + 1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        options[key.strip().lower()] = value
    return options


_COMMAND_LINE_OPTIONS = _parse_command_line_options()

# Option name -> the environment variable(s) that carry the same value. The command line
# tokens use short names (engine, resx, ...) while the environment variables use the DLB_*
# spelling, so both are mapped here instead of at every call site.
ENV_NAMES = {
    "engine": ("DLB_RENDER_ENGINE",),
    "scene": ("DLB_RENDER_SCENE",),
    "layer": ("DLB_VIEW_LAYER",),
    "camera": ("DLB_CAMERA",),
    "format": ("DLB_IMAGE_FORMAT",),
    "resx": ("DLB_RESOLUTION_X",),
    "resy": ("DLB_RESOLUTION_Y",),
    "gpu": ("DLB_GPU_DEVICE",),
    "markers": ("DLB_MARKER_OVERRIDE",),
    "frames": ("DLB_FRAME_START", "DLB_FRAME_END"),
    "output": ("DLB_OUTPUT",),
    "threads": ("DLB_THREADS",),
    "render": ("DLB_RENDER",),
}


def _get(name):
    key = name.lower()
    if key in _COMMAND_LINE_OPTIONS:
        return (_COMMAND_LINE_OPTIONS.get(key) or "").strip()

    env_names = ENV_NAMES.get(key, ())
    if not env_names:
        return ""

    if key == "frames":
        start = os.environ.get(env_names[0], "").strip()
        end = os.environ.get(env_names[1], "").strip()
        if start == "" and end == "":
            return ""
        return "%s-%s" % (start, end)

    return os.environ.get(env_names[0], "").strip()


def _get_int(name, default=0):
    raw = _get(name)
    if raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        raise ValueError("Invalid integer for %s%s: %r" % (ENV_PREFIX, name, raw))


def _log(message):
    print("[deadline-blender-custom] %s" % message, flush=True)


def apply_scene(scene_name, applied):
    if scene_name == "":
        return bpy.context.scene

    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        raise ValueError(
            "Scene %r does not exist in this .blend file. Available scenes: %s"
            % (scene_name, ", ".join(sorted(s.name for s in bpy.data.scenes)))
        )
    bpy.context.window.scene = scene
    applied.append("scene=%s" % scene_name)
    return scene


def apply_engine(scene, logical_engine, applied):
    if logical_engine == "":
        return

    candidates = _engine_candidates(logical_engine)
    if not candidates:
        _log("Unknown render engine %r requested; keeping %s." % (logical_engine, scene.render.engine))
        return

    for candidate in candidates:
        try:
            scene.render.engine = candidate
            applied.append("engine=%s" % candidate)
            return
        except TypeError:
            continue

    _log(
        "None of the engine ids %s are available in Blender %s; keeping %s."
        % (", ".join(candidates), bpy.app.version_string, scene.render.engine)
    )


def apply_view_layer(scene, view_layer_name, applied):
    if view_layer_name == "":
        return

    names = [layer.name for layer in scene.view_layers]
    if view_layer_name not in names:
        raise ValueError(
            "View layer %r does not exist in scene %r. Available view layers: %s"
            % (view_layer_name, scene.name, ", ".join(names))
        )

    for layer in scene.view_layers:
        layer.use = layer.name == view_layer_name
    applied.append("view_layer=%s" % view_layer_name)


def apply_camera(scene, camera_name, applied):
    if camera_name == "":
        return

    camera_object = bpy.data.objects.get(camera_name)
    if camera_object is None:
        raise ValueError("Camera %r does not exist in this .blend file." % camera_name)
    if camera_object.type != "CAMERA":
        raise ValueError("Object %r is a %s, not a camera." % (camera_name, camera_object.type))
    if camera_object.hide_render:
        raise ValueError("Camera %r is not renderable (hide_render is enabled)." % camera_name)

    scene.camera = camera_object
    applied.append("camera=%s" % camera_name)

    if _get("markers") == "1":
        for marker in scene.timeline_markers:
            marker.camera = camera_object
        applied.append("marker_override=on")


def apply_resolution(scene, applied):
    width = _get_int("resx", 0)
    height = _get_int("resy", 0)
    if width <= 0 and height <= 0:
        return
    if width <= 0 or height <= 0:
        raise ValueError(
            "Both resolution values must be greater than 0 when either one is set "
            "(got %s x %s)." % (width, height)
        )

    scene.render.resolution_x = width
    scene.render.resolution_y = height
    applied.append("resolution=%sx%s" % (width, height))


def apply_format(scene, image_format, applied):
    if image_format == "":
        return

    try:
        scene.render.image_settings.file_format = image_format
    except TypeError:
        raise ValueError(
            "Image format %r is not supported by Blender %s."
            % (image_format, bpy.app.version_string)
        )
    applied.append("format=%s" % image_format)


def apply_gpu(scene, requested_device, applied):
    if requested_device == "":
        return

    if scene.render.engine != "CYCLES":
        _log(
            "Cycles GPU device %r requested but the active engine is %s; ignoring."
            % (requested_device, scene.render.engine)
        )
        return

    try:
        cycles_preferences = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        _log("The Cycles add-on is not enabled; cannot set the GPU device.")
        return

    requested_device = requested_device.upper()
    if requested_device == "NONE":
        scene.cycles.device = "CPU"
        applied.append("gpu=CPU (NONE requested)")
        return

    cycles_preferences.refresh_devices()

    device = requested_device
    devices = _compatible_devices(cycles_preferences, device)
    for fallback in GPU_FALLBACK_ORDER + ("NONE",):
        if devices or device == "NONE":
            break
        _log("GPU device type %r is not available on this Worker; trying %r." % (device, fallback))
        device = fallback
        devices = _compatible_devices(cycles_preferences, device)

    if device == "NONE" or not devices:
        scene.cycles.device = "CPU"
        applied.append("gpu=CPU (no requested device available)")
        return

    scene.cycles.device = "GPU"
    cycles_preferences.compute_device_type = device
    applied.append("gpu=%s" % device)


def apply_threads(scene, applied):
    """Apply the job's thread count.

    The plugin also passes "-t" on the command line, but Blender handles its arguments in
    passes, so setting it here as well makes the render independent of that ordering.
    0 means "use the system's processor count", matching Deadline's Threads = 0.
    """
    threads = _get_int("threads", -1)
    if threads < 0:
        return

    if threads == 0:
        scene.render.threads_mode = "AUTO"
        applied.append("threads=AUTO")
    else:
        scene.render.threads_mode = "FIXED"
        scene.render.threads = threads
        applied.append("threads=%s" % threads)


def apply_output(scene, applied):
    output = _get("output")
    if output == "":
        return

    scene.render.filepath = output
    # The stock plugin passed "-x 1" together with "-o", i.e. force the file extension on,
    # and only when an output override was given.
    scene.render.use_file_extension = True

    # Blender does not create the directory it writes into; it just fails. The submission
    # dialog's output presets deliberately use sub folders per scene / view layer, so make
    # sure they exist. Only a path that names a directory is created: the file name part is
    # whatever follows the last separator.
    directory = os.path.dirname(output)
    if directory != "" and not os.path.isdir(directory):
        try:
            os.makedirs(directory)
            _log("Created the output directory %s" % directory)
        except Exception as error:
            _log("Could not create the output directory %s: %s" % (directory, error))

    applied.append("output=%s" % output)


def apply_frame_range(scene, applied):
    raw = _get("frames")
    if raw == "":
        return

    match = FRAME_RANGE_PATTERN.match(raw)
    if match is None:
        raise ValueError("Invalid frame range %r; expected <start>-<end>." % raw)

    start = int(match.group(1))
    end = int(match.group(2))

    scene.frame_start = start
    scene.frame_end = end
    # frame_step is intentionally left as stored in the .blend file: the stock plugin's
    # "-s A -e B -a" let Blender use the scene's own step, and this keeps that behaviour.
    applied.append("frames=%s-%s" % (start, end))


def render_scene(scene):
    """Render the task's frames with the settings applied above.

    This replaces the stock plugin's "-s <start> -e <end> -a" command line flags. Blender
    handles its command line in passes rather than strictly left to right, and the render
    flags are handled in an earlier pass than "--python", so a script that sets up the
    render would otherwise run after the render had already finished.
    """
    _log(
        "Rendering: engine=%s, view_layer=%s, camera=%s, resolution=%sx%s, frames=%s-%s, output=%s"
        % (
            scene.render.engine,
            _get("layer") or "<all enabled layers>",
            scene.camera.name if scene.camera else "<none>",
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.frame_start,
            scene.frame_end,
            scene.render.filepath,
        )
    )

    # Blender prints one "Saved: ..." line per frame, which is what the Deadline plugin
    # counts to report the progress of this task.
    bpy.ops.render.render(animation=True, scene=scene.name)


def _compatible_devices(cycles_preferences, device_type):
    try:
        return cycles_preferences.get_devices_for_type(device_type)
    except (ValueError, TypeError) as error:
        _log("get_devices_for_type(%r) failed: %s" % (device_type, error))
        return []


def main():
    applied = []

    _log("Blender %s, options received: %s" % (bpy.app.version_string, _COMMAND_LINE_OPTIONS if _COMMAND_LINE_OPTIONS else "<none on the command line, using environment variables>"))

    scene = apply_scene(_get("scene"), applied)
    apply_engine(scene, _get("engine"), applied)
    apply_view_layer(scene, _get("layer"), applied)
    apply_camera(scene, _get("camera"), applied)
    apply_resolution(scene, applied)
    apply_format(scene, _get("format"), applied)
    apply_gpu(scene, _get("gpu"), applied)
    apply_threads(scene, applied)
    apply_output(scene, applied)
    apply_frame_range(scene, applied)

    if applied:
        _log("Applied: %s" % ", ".join(applied))
    else:
        _log("No render option overrides requested; using the settings stored in the .blend file.")

    render_option = _get("render")
    if render_option == "1":
        render_scene(scene)
    elif render_option == "0":
        _log("render=0 was passed; settings applied without rendering.")
    else:
        # Fail loudly instead of exiting successfully without having rendered anything.
        raise RuntimeError(
            "No render options were passed to BlenderRenderScript.py: the Blender command line "
            "has no '-- render=1 ...' tokens and the DLB_* environment variables are empty. "
            "The Worker is most likely running an older copy of plugins/Blender/Blender.py - "
            "compare the 'Blender command line:' line in the task log with the one this script "
            "expects. Refusing to finish without rendering."
        )


# Run unconditionally: Blender executes this file with --python, and the settings plus the
# render itself depend on it running. Relying on "if __name__ == '__main__'" would depend
# on how Blender sets up the module globals.
try:
    main()
except Exception as error:
    # Make the failure obvious in the task log and let --python-exit-code turn it
    # into a non-zero exit status so Deadline fails the task.
    print("[deadline-blender-custom] ERROR: %s" % error, flush=True)
    raise
