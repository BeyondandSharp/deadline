"""Monitor job script: inspect and change the Blender render options of a submitted job.

    Monitor -> Jobs panel -> right-click a job -> Scripts -> Blender Render Options...

The scene, view layer and camera drop-downs are filled from the .blend file the job points
at. That read goes through ``custom/lib/blend_names.py``, which uses the vendored Blender
Asset Tracer parser: it reads the file's block index and its embedded SDNA and **never
starts Blender and never builds scene data**, so a 15 MB file is answered in ~0.015 s and a
multi-GB file is not read across the network. If the file is ZStandard-compressed (Blender
5.2 compresses .blend files by default) the names are read after decompressing, or - only
as a last resort - by asking the Blender executable named below, which does load the file.

Saving writes the same plugin-info keys that the Blender submission dialog writes and that
custom/plugins/Blender/Blender.py reads on the Worker, so the change applies to the tasks
the job has not started yet.

Repository:  <DeadlineRepository>/custom/scripts/Jobs/blender_render_options.py
"""

from __future__ import absolute_import

import os
import sys
import time
import traceback

from Deadline.Scripting import MonitorUtils, RepositoryUtils
from DeadlineUI.Controls.Scripting.DeadlineScriptDialog import DeadlineScriptDialog

USE_SCENE_SETTING = "Use Scene Setting"

ENGINE_ITEMS = (USE_SCENE_SETTING, "cycles", "eevee", "workbench")

IMAGE_FORMAT_ITEMS = (
    USE_SCENE_SETTING, "BMP", "CINEON", "DDS", "DPX", "HDR", "IRIS", "JPEG", "JPEG2000",
    "OPEN_EXR", "OPEN_EXR_MULTILAYER", "PNG", "TARGA", "TARGA_RAW", "TIFF", "WEBP",
    "AVIF", "AVI_JPEG", "AVI_RAW", "FFMPEG",
)

GPU_DEVICE_ITEMS = (USE_SCENE_SETTING, "NONE", "CUDA", "OPTIX", "HIP", "ONEAPI", "METAL")

# Plugin info keys this dialog owns. Everything else (Version, VersionFull, Threads,
# Build, AvailableScenes, ...) is left untouched.
TEXT_KEYS = (
    ("SceneBox", "RenderScene"),
    ("ViewLayerBox", "ViewLayer"),
    ("CameraBox", "Camera"),
    ("EngineBox", "RenderEngine"),
    ("FormatBox", "ImageFormat"),
    ("GpuBox", "GpuDevice"),
)
NUMBER_KEYS = (
    ("ResolutionXBox", "ResolutionX"),
    ("ResolutionYBox", "ResolutionY"),
)
CHECK_KEYS = (
    ("StrictErrorBox", "StrictErrorChecking"),
    ("MarkerOverrideBox", "MarkerOverride"),
)

scriptDialog = None
infoDialog = None
selectedJobs = []
blendPath = ""
blendNames = None
readError = ""
sceneBox = None


########################################################################
## Helpers
########################################################################
def RepositoryPath(relativePath):
    """Resolve a repository file through Deadline.

    This is not just a path lookup: the Repository Cache Service fetches a file **when it is
    asked for it**, so calling this pulls the file into the cache. A cached repository only
    contains what Deadline itself has requested, which is why Python cannot simply import
    our files from a __file__-relative path.
    """
    try:
        return str(RepositoryUtils.GetRepositoryFilePath(relativePath, True))
    except Exception as error:
        Log("could not resolve %s: %s" % (relativePath, error))
        return ""


def LibDirectory():
    """custom/lib - resolved through Deadline first, then relative to this file."""
    readerPath = RepositoryPath("lib/blend_names.py")
    if readerPath and os.path.isfile(readerPath):
        return os.path.dirname(readerPath)

    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))


def LoadBlendNamesModule():
    """Import custom/lib/blend_names.py, after fetching it and its parser into the cache."""
    libDirectory = LibDirectory()
    if libDirectory not in sys.path:
        sys.path.insert(0, libDirectory)

    import blend_names

    for relativePath in blend_names.repository_files():
        RepositoryPath(relativePath)

    Log("reader loaded from %s" % libDirectory)
    return blend_names


def SettingsFile():
    try:
        from Deadline.Scripting import ClientUtils
        return os.path.join(ClientUtils.GetUsersSettingsDirectory(), "BlenderRenderOptions.ini")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "BlenderRenderOptions.ini")


def LoadSetting(key, default=""):
    try:
        with open(SettingsFile(), "r") as handle:
            for line in handle:
                if line.strip().startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def SaveSetting(key, value):
    try:
        settings = {}
        try:
            with open(SettingsFile(), "r") as handle:
                for line in handle:
                    if "=" in line:
                        settings[line.split("=", 1)[0].strip()] = line.split("=", 1)[1].strip()
        except Exception:
            pass
        settings[key] = value
        with open(SettingsFile(), "w") as handle:
            for name in sorted(settings):
                handle.write("%s=%s\n" % (name, settings[name]))
    except Exception:
        pass


def DefaultBlenderExecutable():
    """Blender to use for the compressed-file fallback: remembered value, env, then a guess."""
    remembered = LoadSetting("blender_executable")
    if remembered and os.path.isfile(remembered):
        return remembered

    for variable in ("BAT_BLENDER", "BLENDER_EXECUTABLE"):
        candidate = os.environ.get(variable, "")
        if candidate and os.path.isfile(candidate):
            return candidate

    if os.name == "nt":
        import glob
        found = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"))
        if found:
            return found[-1]

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "blender")
        if os.path.isfile(candidate):
            return candidate

    return ""


def JobBlendPath(job):
    """The .blend of a job: the SceneFile plugin info, or the copy submitted with the job."""
    sceneFile = job.GetJobPluginInfoKeyValue("SceneFile")
    if sceneFile:
        return sceneFile

    # "Submit Blender Scene File With The Job" turns the .blend into an auxiliary file.
    try:
        auxDirectory = RepositoryUtils.GetJobAuxiliaryPath(job)
        for name in job.JobAuxiliarySubmissionFileNames:
            if name.lower().endswith(".blend"):
                return os.path.join(auxDirectory, name)
    except Exception:
        pass

    return ""


def PluginValue(job, key, default=""):
    try:
        value = job.GetJobPluginInfoKeyValue(key)
        return value if value else default
    except Exception:
        return default


########################################################################
## Reading the names
########################################################################
def SplitList(value):
    """The submitter records name lists as one comma separated string."""
    return [ item.strip() for item in str(value or "").split(",") if item.strip() ]


def UseJobRecordedNames():
    """Fill the drop-downs from the lists the submitter stored in the job itself.

    The submission dialog writes AvailableScenes / AvailableViewLayers / AvailableCameras, so
    for a job submitted from Blender the dialog can be filled **without touching the network
    at all**. Reading the .blend is left to the "Read From File" button, because that file can
    live on a slow or unreachable share and must never block the dialog from appearing.
    """
    global blendNames

    job = selectedJobs[0]
    scenes = SplitList(PluginValue(job, "AvailableScenes"))
    viewLayers = SplitList(PluginValue(job, "AvailableViewLayers"))
    cameras = SplitList(PluginValue(job, "AvailableCameras"))

    if not (scenes or viewLayers or cameras):
        blendNames = None
        return False

    blendNames = {
        # Per-scene layers are unknown here; empty means "do not validate the combination".
        "scenes": dict((name, []) for name in scenes),
        "view_layers": viewLayers,
        "cameras": cameras,
        "source": "the job's recorded lists",
        "seconds": 0.0,
    }
    return True


def ReadNamesFromBlendFile():
    """Read the names out of the .blend with custom/lib/blend_names.py.

    Called from the "Read From File" button only: it is the one step that touches the file,
    which may be on a slow share. Both the start and the outcome are logged, so a hang or a
    failure can be told apart from a silent return.
    """
    global blendNames, readError, blendPath

    readError = ""
    blendPath = JobBlendPath(selectedJobs[0])
    Log("read from file requested: %s" % (blendPath or "<no .blend recorded for this job>"))

    if not blendPath:
        readError = ("This job has no SceneFile and no .blend among its auxiliary files, so the "
                     "scene / view layer / camera lists cannot be filled from a file.")
        Log(readError)
        return False

    if not os.path.isfile(blendPath):
        readError = "The .blend file does not exist (or is not reachable): %s" % blendPath
        Log(readError)
        return False

    try:
        names = LoadBlendNamesModule().read_names(
            blendPath, blender_exe=LoadSetting("blender_executable") or None)
    except Exception as error:
        readError = "%s" % error
        Log("reading failed: %s" % error)
        return False

    viewLayers = sorted(set(layer for layers in names["scenes"].values() for layer in layers))
    blendNames = {
        "scenes": names["scenes"],
        "view_layers": viewLayers,
        "cameras": names["cameras"],
        "source": names["source"],
        "seconds": names["seconds"],
    }
    Log("read %d scene(s) with %s in %.3fs" % (len(names["scenes"]), names["source"], names["seconds"]))
    return True


def LayersForScene(sceneName):
    if not blendNames:
        return [USE_SCENE_SETTING]

    scenes = blendNames["scenes"]
    layers = scenes.get(sceneName) or blendNames.get("view_layers") or []
    if not layers:
        # Union of every scene, so a scene with unknown layers still offers something.
        unique = set()
        for sceneLayers in scenes.values():
            unique.update(sceneLayers)
        layers = sorted(unique)
    return [USE_SCENE_SETTING] + list(layers)


def NamesFor(kind):
    if not blendNames:
        return [USE_SCENE_SETTING]
    if kind == "scenes":
        return [USE_SCENE_SETTING] + sorted(blendNames["scenes"].keys())
    return [USE_SCENE_SETTING] + list(blendNames["cameras"])


########################################################################
## Diagnostics
########################################################################
def LogFile():
    return os.path.join(os.path.dirname(SettingsFile()), "BlenderRenderOptions.log")


def Log(message):
    """Append to a log next to the user's settings.

    A job script that fails before its dialog is shown leaves no visible trace in the
    Monitor, so every run writes what it did here.
    """
    try:
        with open(LogFile(), "a") as handle:
            handle.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def ShowInfo(title, text):
    """A dialog that is guaranteed to appear.

    ``DeadlineScriptDialog().ShowMessageBox()`` on its own does nothing when the dialog was
    never shown, which is how an early return used to end in complete silence.
    """
    global infoDialog

    infoDialog = DeadlineScriptDialog()
    infoDialog.SetTitle(title)
    infoDialog.SetSize(640, 220)
    infoDialog.AllowResizingDialog(True)
    infoDialog.AddGrid()
    infoDialog.AddControlToGrid("InfoLabel", "LabelControl", text, 0, 0, "", False)
    closeButton = infoDialog.AddControlToGrid("CloseButton", "ButtonControl", "Close", 2, 0, expand=False)
    closeButton.ValueModified.connect(CloseInfo)
    infoDialog.EndGrid()
    infoDialog.ShowDialog(False)


def CloseInfo(*args):
    global infoDialog
    if infoDialog is not None:
        infoDialog.CloseDialog()


########################################################################
## Dialog
########################################################################
def __main__(*args):
    """Entry point called by the Monitor.

    Nothing may escape from here. An exception raised out of __main__ only ends up in the
    Monitor's error output - no dialog, no log - which is exactly how this script failed
    silently at first: it asked for job.JobPluginName, a property that does not exist (the
    API property is job.JobPlugin).
    """
    try:
        RunScript()
    except Exception:
        Log("unhandled error:\n%s" % traceback.format_exc())
        ShowInfo("Blender Render Options - error",
                 "The script stopped with an error:\n\n%s" % traceback.format_exc())


def RunScript():
    global scriptDialog, selectedJobs, sceneBox

    Log("---- run requested")

    try:
        selectedJobs = list(MonitorUtils.GetSelectedJobs())
    except Exception as error:
        Log("GetSelectedJobs failed: %s" % error)
        ShowInfo("Blender Render Options",
                 "This script has to be run from the Monitor's Jobs panel:\n\n"
                 "    right-click a job  ->  Scripts  ->  Blender Render Options\n\n"
                 "It cannot run from the Launcher or from deadlinecommand (%s)." % error)
        return

    Log("selected jobs: %d" % len(selectedJobs))
    if not selectedJobs:
        ShowInfo("Blender Render Options",
                 "No job is selected.\n\nClick a job in the Jobs panel first - a right-click does not "
                 "always select the row - and then run this script again.")
        return

    nonBlenderJobs = [job.JobName for job in selectedJobs if job.JobPlugin != "Blender"]
    if nonBlenderJobs:
        Log("not Blender jobs: %s" % ", ".join(nonBlenderJobs))
        ShowInfo("Blender Render Options",
                 "These jobs do not use the Blender plugin and cannot be edited here:\n\n  %s"
                 % "\n  ".join(nonBlenderJobs))
        return

    Log("all selected jobs use the Blender plugin")
    BuildDialog()


def BuildDialog():
    global scriptDialog, sceneBox

    job = selectedJobs[0]
    Log("building dialog for job %s" % job.JobName)

    # Instant, in-memory lists recorded by the submitter. The .blend itself is only read when
    # the user presses "Read From File" below - a job's .blend can sit on a share that takes
    # minutes to answer, and the dialog must appear regardless.
    Log("job recorded name lists: %s" % ("yes" if UseJobRecordedNames() else "no"))

    scriptDialog = DeadlineScriptDialog()
    scriptDialog.SetTitle("Blender Render Options")
    scriptDialog.AllowResizingDialog(True)
    scriptDialog.SetSize(720, 460)

    scriptDialog.AddGrid()
    scriptDialog.AddControlToGrid("StatusLabel", "LabelControl", StatusText(), 0, 0, StatusTooltip(), False)
    scriptDialog.AddControlToGrid("StatusBox", "LabelControl", StatusText(), 0, 1, StatusTooltip(), False)
    scriptDialog.EndGrid()

    scriptDialog.AddGrid()
    row = 0

    sceneBox = scriptDialog.AddComboControlToGrid(
        "SceneBox", "ComboControl", PluginValue(job, "RenderScene") or USE_SCENE_SETTING,
        tuple(NamesFor("scenes")), row, 1, expand=False)
    scriptDialog.AddControlToGrid("SceneLabel", "LabelControl", "Scene", row, 0,
                                  "The scene to render. The list comes from the submitted .blend file.", False)
    sceneBox.ValueModified.connect(SceneChanged)

    row += 1
    scriptDialog.AddControlToGrid("ViewLayerLabel", "LabelControl", "View Layer", row, 0,
                                  "The view layer to render; all other layers of the scene are disabled for the render.", False)
    scriptDialog.AddComboControlToGrid(
        "ViewLayerBox", "ComboControl", PluginValue(job, "ViewLayer") or USE_SCENE_SETTING,
        tuple(LayersForScene(PluginValue(job, "RenderScene") or USE_SCENE_SETTING)), row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("CameraLabel", "LabelControl", "Camera", row, 0,
                                  "The camera to render.", False)
    scriptDialog.AddComboControlToGrid(
        "CameraBox", "ComboControl", PluginValue(job, "Camera") or USE_SCENE_SETTING,
        tuple(NamesFor("cameras")), row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("EngineLabel", "LabelControl", "Render Engine", row, 0,
                                  "Overrides the render engine at render time.", False)
    scriptDialog.AddComboControlToGrid(
        "EngineBox", "ComboControl", PluginValue(job, "RenderEngine") or USE_SCENE_SETTING,
        ENGINE_ITEMS, row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("FormatLabel", "LabelControl", "Image Format", row, 0,
                                  "Overrides the output image format.", False)
    scriptDialog.AddComboControlToGrid(
        "FormatBox", "ComboControl", PluginValue(job, "ImageFormat") or USE_SCENE_SETTING,
        IMAGE_FORMAT_ITEMS, row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("GpuLabel", "LabelControl", "Cycles GPU Device", row, 0,
                                  "Cycles compute device: NONE forces CPU rendering. Only used by the Cycles engine.", False)
    scriptDialog.AddComboControlToGrid(
        "GpuBox", "ComboControl", PluginValue(job, "GpuDevice") or USE_SCENE_SETTING,
        GPU_DEVICE_ITEMS, row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("ResolutionLabel", "LabelControl", "Resolution X / Y", row, 0,
                                  "Overrides the render resolution. 0 in both keeps the resolution stored in the .blend file.", False)
    scriptDialog.AddRangeControlToGrid(
        "ResolutionXBox", "RangeControl", int(PluginValue(job, "ResolutionX", "0") or 0),
        0, 65536, 0, 1, row, 1, expand=False)
    scriptDialog.AddRangeControlToGrid(
        "ResolutionYBox", "RangeControl", int(PluginValue(job, "ResolutionY", "0") or 0),
        0, 65536, 0, 1, row, 2, expand=False)

    row += 1
    scriptDialog.AddSelectionControlToGrid(
        "StrictErrorBox", "CheckBoxControl",
        bool(PluginValue(job, "StrictErrorChecking")), "Strict Error Checking", row, 1,
        "Fail the task when the render log matches the pattern configured in the Blender plugin settings.")
    scriptDialog.AddSelectionControlToGrid(
        "MarkerOverrideBox", "CheckBoxControl",
        bool(PluginValue(job, "MarkerOverride")), "Override Camera Markers", row, 2,
        "When a specific camera is selected, also rebind every timeline marker to it.")

    row += 1
    scriptDialog.AddControlToGrid("BlenderExeLabel", "LabelControl", "Blender Executable", row, 0,
                                  "Only used for ZStandard-compressed .blend files when no zstd module is available; "
                                  "reading the names normally does not start Blender at all.", False)
    scriptDialog.AddSelectionControlToGrid(
        "BlenderExeBox", "FileBrowserControl", DefaultBlenderExecutable(),
        "Blender (blender.exe blender);;All Files (*)", row, 1, colSpan=2)

    row += 1
    scriptDialog.AddControlToGrid("JobsLabel", "LabelControl", "Applies to", row, 0,
                                  "The options are written to every selected Blender job.", False)
    scriptDialog.AddControlToGrid("JobsBox", "LabelControl",
                                  "%d selected job(s)" % len(selectedJobs), row, 1, "", False)

    row += 1
    readButton = scriptDialog.AddControlToGrid("ReadButton", "ButtonControl", "Read From File", row, 1, expand=False)
    readButton.ValueModified.connect(ReadFromFilePressed)
    scriptDialog.AddControlToGrid("ReadHintLabel", "LabelControl", "", row, 2,
                                  "Reads the scene, view layer and camera names out of the job's .blend file. "
                                  "It never loads the file: only its block index and structure catalog are read. "
                                  "Use it when this job has no recorded lists (a job submitted before the "
                                  "custom submitter, or from the Monitor).", False)

    saveButton = scriptDialog.AddControlToGrid("SaveButton", "ButtonControl", "Save", row + 1, 1, expand=False)
    saveButton.ValueModified.connect(SavePressed)
    closeButton = scriptDialog.AddControlToGrid("CloseButton", "ButtonControl", "Close", row + 1, 2, expand=False)
    closeButton.ValueModified.connect(ClosePressed)
    scriptDialog.EndGrid()

    scriptDialog.ShowDialog(False)
    Log("dialog shown")


def ReadFromFilePressed(*args):
    """The one step that touches the network: read the names out of the .blend."""
    global readError

    scriptDialog.SetValue("StatusBox", "Reading %s ..." % (JobBlendPath(selectedJobs[0]) or "?"))

    if not ReadNamesFromBlendFile():
        scriptDialog.SetValue("StatusBox", "Read failed - see tooltip / log")
        scriptDialog.ShowMessageBox(
            "%s\n\nThe drop-downs keep their current values. You can also set the names by hand in "
            "Job Properties -> Blender Settings." % (readError or "The .blend could not be read."),
            "Blender Render Options")
        return

    scriptDialog.SetValue("StatusBox", StatusText())
    scriptDialog.SetItems("SceneBox", tuple(NamesFor("scenes")))
    scriptDialog.SetItems("CameraBox", tuple(NamesFor("cameras")))
    SceneChanged()
    Log("drop-downs refilled from the file")


def StatusText():
    if readError:
        return "Names unavailable - see tooltip"
    if not blendNames:
        return "No name lists available - press 'Read From File'"
    if blendNames["source"].startswith("the job"):
        return "Names from the job's recorded lists (no file access)"
    return "Names from the .blend, read by %s in %.3fs" % (blendNames["source"], blendNames["seconds"])


def StatusTooltip():
    lines = []
    if readError:
        lines.append(readError)
    if blendPath:
        lines.append("File: %s" % blendPath)
    if blendNames:
        lines.append("Source: %s" % blendNames["source"])
        lines.append("Scenes: %s" % ", ".join(sorted(blendNames["scenes"].keys())))
        layers = blendNames.get("view_layers") or []
        lines.append("View layers: %s" % (", ".join(layers) or "<unknown>"))
        lines.append("Cameras: %s" % (", ".join(blendNames["cameras"]) or "<none>"))
    if not lines:
        lines.append("The names are only read when you press 'Read From File'.")
    return "\n".join(lines)


def SceneChanged(*args):
    """Re-fill the view layer list for the newly selected scene."""
    scene = scriptDialog.GetValue("SceneBox")
    scriptDialog.SetItems("ViewLayerBox", tuple(LayersForScene(scene)))
    scriptDialog.SetValue("ViewLayerBox", USE_SCENE_SETTING)


########################################################################
## Saving
########################################################################
def Validate():
    scene = scriptDialog.GetValue("SceneBox")
    layer = scriptDialog.GetValue("ViewLayerBox")
    camera = scriptDialog.GetValue("CameraBox")

    if blendNames:
        if scene not in (USE_SCENE_SETTING, "") and scene not in blendNames["scenes"]:
            return "The scene '%s' is not in the submitted .blend file." % scene
        if layer not in (USE_SCENE_SETTING, ""):
            layers = blendNames["scenes"].get(scene, [])
            if layers and layer not in layers:
                return "The view layer '%s' is not in scene '%s'." % (layer, scene)
        if camera not in (USE_SCENE_SETTING, "") and camera not in blendNames["cameras"]:
            return "The camera '%s' is not in the submitted .blend file." % camera

    resolutionX = int(scriptDialog.GetValue("ResolutionXBox"))
    resolutionY = int(scriptDialog.GetValue("ResolutionYBox"))
    if (resolutionX > 0) != (resolutionY > 0):
        return "Set both Resolution X and Resolution Y, or leave both at 0."

    if len(selectedJobs) > 1:
        files = set(JobBlendPath(job) for job in selectedJobs)
        if len(files) > 1 and blendNames:
            return ("The selected jobs do not all use the same .blend file, so the scene / view "
                    "layer / camera names above cannot be valid for all of them. Select jobs that "
                    "share a file, or clear those three fields before saving.")

    return ""


def SavePressed(*args):
    global scriptDialog

    problem = Validate()
    if problem:
        scriptDialog.ShowMessageBox(problem, "Blender Render Options")
        return

    blenderExecutable = scriptDialog.GetValue("BlenderExeBox")
    if blenderExecutable:
        SaveSetting("blender_executable", blenderExecutable)

    try:
        for job in selectedJobs:
            for controlName, pluginInfoKey in TEXT_KEYS:
                value = str(scriptDialog.GetValue(controlName))
                if value == USE_SCENE_SETTING:
                    value = ""
                job.SetJobPluginInfoKeyValue(pluginInfoKey, value)

            for controlName, pluginInfoKey in NUMBER_KEYS:
                value = int(scriptDialog.GetValue(controlName))
                job.SetJobPluginInfoKeyValue(pluginInfoKey, str(value) if value > 0 else "")

            for controlName, pluginInfoKey in CHECK_KEYS:
                job.SetJobPluginInfoKeyValue(
                    pluginInfoKey, "True" if scriptDialog.GetValue(controlName) else "")

            RepositoryUtils.SaveJob(job)

        scriptDialog.ShowMessageBox(
            "Updated %d job(s).\n\nThe new options apply to the tasks that have not started yet; "
            "suspend and resume a running job (or enable 'Reload Plugin Between Tasks' for it) to "
            "have it pick them up immediately." % len(selectedJobs),
            "Blender Render Options")
    except Exception:
        scriptDialog.ShowMessageBox(traceback.format_exc(), "Blender Render Options")


def ClosePressed(*args):
    global scriptDialog
    scriptDialog.CloseDialog()
