#
# CUSTOM OVERRIDE of the stock Blender-side submission proxy.
#
# Repository:  <DeadlineRepository>/custom/submission/Blender/Main/SubmitBlenderToDeadline.py
# Factory file: <DeadlineRepository>/submission/Blender/Main/SubmitBlenderToDeadline.py  (left untouched)
#
# The Blender add-on (DeadlineBlenderClient.py, already installed on every artist
# machine) resolves the repository path for "submission/Blender/Main" through
# "deadlinecommand -GetRepositoryPath", which checks the repository's "custom" folder
# first. No artist-side reinstall is therefore needed: this file replaces the stock
# proxy automatically.
#
# On top of the stock proxy it collects the information about the open .blend file that
# the submission dialog cannot obtain on its own (the dialog runs in a separate
# deadlinecommand process) and passes it along as a 6th argument, base64 encoded so
# that spaces, quotes and non-ASCII characters in scene/layer/camera names survive the
# command line untouched.
#
# Besides the names of the scene being submitted ("scenes", "view_layers", "cameras") it
# sends "scene_details": every scene's view layers and cameras. The dialog's "All View
# Layers" / "All Cameras" options ("one job per view layer / camera") need the names of
# whichever scene is selected, not only the active one.
#
#Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#This program is free software: you can redistribute it and/or modify
#it under the terms of the GNU General Public License as published by
#the Free Software Foundation, either version 2 of the License, or
#(at your option) any later version.
#
#This program is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU General Public License for more details.
#
#You should have received a copy of the GNU General Public License
#along with this program.  If not, see <http://www.gnu.org/licenses/>.

import bpy
import base64
import json
import os
import subprocess

def GetDeadlineCommand():
    deadlineBin = ""
    try:
        deadlineBin = os.environ['DEADLINE_PATH']
    except KeyError:
        #if the error is a key error it means that DEADLINE_PATH is not set. however Deadline command may be in the PATH or on OSX it could be in the file /Users/Shared/Thinkbox/DEADLINE_PATH
        pass

    # On OSX, we look for the DEADLINE_PATH file if the environment variable does not exist.
    if deadlineBin == "" and  os.path.exists( "/Users/Shared/Thinkbox/DEADLINE_PATH" ):
        with open( "/Users/Shared/Thinkbox/DEADLINE_PATH" ) as f:
            deadlineBin = f.read().strip()

    deadlineCommand = os.path.join(deadlineBin, "deadlinecommand")

    return deadlineCommand

def GetRepositoryFilePath(subdir):
    deadlineCommand = GetDeadlineCommand()

    startupinfo = None
    #if os.name == 'nt':
    #   startupinfo = subprocess.STARTUPINFO()
    #   startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    args = [deadlineCommand, "-GetRepositoryFilePath "]
    if subdir != None and subdir != "":
        args.append(subdir)

    # Specifying PIPE for all handles to workaround a Python bug on Windows. The unused handles are then closed immediatley afterwards.
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)

    proc.stdin.close()
    proc.stderr.close()

    output = proc.stdout.read()

    path = output.decode("utf_8")
    path = path.replace("\r","").replace("\n","").replace("\\","/")

    return path

def GetBlenderVersion():
    """Return the major.minor version, which is the key used to pick the render executable."""
    return ".".join( str( part ) for part in bpy.app.version[:2] )

def GetBlenderVersionFull():
    """Return the full numeric version (4.2.1), used by the {version_full} path placeholder."""
    return ".".join( str( part ) for part in bpy.app.version[:3] )

def GetCyclesGpuDevice(scene):
    """Return the Cycles compute device type, or NONE when the scene renders on the CPU."""
    try:
        if scene.cycles.device != "GPU":
            return "NONE"
        return bpy.context.preferences.addons["cycles"].preferences.compute_device_type or "NONE"
    except Exception:
        return "NONE"

def GetRenderableCameras(scene):
    """Return the cameras of the scene, preferring the ones that are actually renderable."""
    all_cameras = [ obj.name for obj in scene.objects if obj.type == "CAMERA" ]
    renderable = [ obj.name for obj in scene.objects if obj.type == "CAMERA" and not obj.hide_render ]
    return renderable if renderable else all_cameras

def GetSceneDetails():
    """Per-scene view layers, cameras and render settings, for the submission dialog.

    The flat "view_layers", "cameras" and "engine" entries below describe the scene that is being
    submitted; the dialog also offers a tree of every scene with one job per ticked combination,
    which can target a scene other than the active one. These are the values a row shows when its
    setting says "Use Scene Setting".
    """
    return dict(
        ( scene.name, {
            "view_layers": [ layer.name for layer in scene.view_layers ],
            "cameras": GetRenderableCameras( scene ),
            "engine": str( scene.render.engine ),
            "resolution_x": int( scene.render.resolution_x ),
            "resolution_y": int( scene.render.resolution_y ),
            "image_format": str( scene.render.image_settings.file_format ),
            "pixel_aspect_x": float( scene.render.pixel_aspect_x ),
            "pixel_aspect_y": float( scene.render.pixel_aspect_y ),
        } )
        for scene in bpy.data.scenes
    )

def GetBlenderLanguage():
    """The language Blender itself is running in, for the submission dialog's translations.

    bpy.app.translations.locale is the language actually in use (it follows the user preference
    and reflects any command line override); the preference is the fallback.
    """
    try:
        locale = str(bpy.app.translations.locale)
        if locale != "" and locale != "en_US":
            return locale
    except Exception:
        pass

    try:
        return str(bpy.context.preferences.view.language)
    except Exception:
        return ""


def GetActiveViewLayer(scene):
    """The view layer the artist is currently looking at.

    The tree in the submission dialog ticks it (with the active camera) by default, so the
    default submission renders exactly what Blender shows.
    """
    try:
        return str(bpy.context.view_layer.name)
    except Exception:
        pass

    try:
        return str(scene.view_layers.active.name)
    except Exception:
        return ""

def BuildSubmitContext(scene):
    """Collect everything the submission dialog needs to know about the open file."""
    return {
        "version": GetBlenderVersion(),
        "version_full": GetBlenderVersionFull(),
        "engine": str( scene.render.engine ),
        "language": GetBlenderLanguage(),
        "active_scene": scene.name,
        "scenes": [ s.name for s in bpy.data.scenes ],
        "view_layers": [ layer.name for layer in scene.view_layers ],
        "cameras": GetRenderableCameras( scene ),
        "active_camera": scene.camera.name if scene.camera else "",
        "active_view_layer": GetActiveViewLayer( scene ),
        "gpu_device": GetCyclesGpuDevice( scene ),
        "resolution_x": int( scene.render.resolution_x ),
        "resolution_y": int( scene.render.resolution_y ),
        "image_format": str( scene.render.image_settings.file_format ),
        "scene_details": GetSceneDetails(),
    }

def EncodeSubmitContext(scene):
    """Encode the context as base64 so the command line cannot mangle it."""
    try:
        payload = json.dumps( BuildSubmitContext( scene ) )
        return base64.b64encode( payload.encode( "utf_8" ) ).decode( "ascii" )
    except Exception as error:
        # Never block a submission because the optional context could not be built -
        # the dialog simply falls back to manual entry.
        print( "Could not build the Deadline submit context: %s" % error )
        return ""

def main( ):
    script_file = GetRepositoryFilePath("scripts/Submission/BlenderSubmission.py")

    curr_scene = bpy.context.scene
    curr_render = curr_scene.render

    scene_file = str(bpy.data.filepath)

    if scene_file != "":
        bpy.ops.wm.save_mainfile()

    frame_range = str(curr_scene.frame_start)
    if curr_scene.frame_start != curr_scene.frame_end:
        frame_range = frame_range + "-" + str(curr_scene.frame_end)

    output_path = str(curr_render.frame_path( frame=curr_scene.frame_start ))
    threads_mode = str(curr_render.threads_mode)
    threads = curr_render.threads
    if threads_mode == "AUTO":
        threads = 0

    platform = str(bpy.app.build_platform)

    submit_context = EncodeSubmitContext( curr_scene )

    deadlineCommand = GetDeadlineCommand()

    args = []
    args.append(deadlineCommand)
    args.append("-ExecuteScript")
    args.append(script_file)
    args.append(scene_file)
    args.append(frame_range)
    args.append(output_path)
    args.append(str(threads))
    args.append(platform)
    args.append(submit_context)

    startupinfo = None
    #~ if os.name == 'nt':
        #~ startupinfo = subprocess.STARTUPINFO()
        #~ startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    subprocess.Popen(args, startupinfo=startupinfo)
