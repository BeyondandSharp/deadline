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

# The two resolution spin boxes of the Qt cell (see AddResolutionCell): set when Qt is available,
# None when the dialog falls back to the two Deadline controls of the grid.
resolutionXSpin = None
resolutionYSpin = None
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
    closeButton = infoDialog.AddControlToGrid("CloseButton", "ButtonControl", Translate( "Close" ), 2, 0, expand=False)
    closeButton.ValueModified.connect(CloseInfo)
    infoDialog.EndGrid()
    infoDialog.ShowDialog(False)


def CloseInfo(*args):
    global infoDialog
    if infoDialog is not None:
        infoDialog.CloseDialog()


########################################################################
## The resolution X / Y cell
########################################################################
def AddResolutionCell( grid, row, initialX, initialY ):
    """Put the two resolution fields next to each other, flush, and return (x, y) spin boxes.

    The dialog's grid always leaves a gap between its columns, so the pair is built as one Qt
    cell with no spacing. Returns (None, None) when Qt is not available; the caller then falls
    back to two ordinary Deadline controls.
    """
    global resolutionXSpin, resolutionYSpin

    try:
        from PyQt5 import QtCore, QtWidgets

        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout( container )
        layout.setContentsMargins( 0, 0, 0, 0 )
        layout.setSpacing( 0 )

        resolutionXSpin = QtWidgets.QSpinBox()
        resolutionYSpin = QtWidgets.QSpinBox()
        for spin in ( resolutionXSpin, resolutionYSpin ):
            spin.setRange( 0, 65536 )
            spin.setMinimumWidth( 90 )

        resolutionXSpin.setValue( max( 0, int( initialX or 0 ) ) )
        resolutionXSpin.setToolTip( "Render resolution X. 0 in both keeps the resolution stored in "
                                   "the .blend file." )
        resolutionYSpin.setValue( max( 0, int( initialY or 0 ) ) )
        resolutionYSpin.setToolTip( "Render resolution Y. 0 in both keeps the resolution stored in "
                                   "the .blend file." )

        layout.addWidget( resolutionXSpin )
        layout.addWidget( resolutionYSpin )

        grid.addWidget( container, row, 1, 1, 2 )

        return resolutionXSpin, resolutionYSpin
    except Exception:
        resolutionXSpin = None
        resolutionYSpin = None
        return None, None


def ResolutionValues():
    """(x, y) as they are set right now, whichever kind of control the dialog got."""
    if resolutionXSpin is not None and resolutionYSpin is not None:
        return int( resolutionXSpin.value() ), int( resolutionYSpin.value() )

    return int( scriptDialog.GetValue( "ResolutionXBox" ) ), int( scriptDialog.GetValue( "ResolutionYBox" ) )


def ResolutionOverridden():
    # type: () -> bool
    return resolutionXSpin is not None and resolutionYSpin is not None


########################################################################
## Translations
########################################################################
# The dialog follows the language of the operating system: this script runs inside the Monitor,
# which has no Blender to ask (the submission dialog opened from Blender uses Blender's language
# instead). DLB_LANGUAGE overrides it, which is also how the tests pick one.
LANGUAGE_ENVIRONMENT_VARIABLE = "DLB_LANGUAGE"

TRANSLATIONS_ZH = {
    # The dialog itself
    "Blender Render Options": u"Blender 渲染选项",
    "Blender Render Options - error": u"Blender 渲染选项 - 错误",
    "READ FROM FILE": u"从文件读取",
    "DONE": u"完成",
    "FAILED": u"失败",
    "Save": u"保存",
    "Close": u"关闭",
    "Scene": u"场景",
    "View Layer": u"渲染层",
    "Camera": u"相机",
    "Render Engine": u"渲染引擎",
    "Image Format": u"图像格式",
    "Cycles GPU Device": u"Cycles GPU 设备",
    "Resolution X / Y": u"分辨率 X / Y",
    "Strict Error Checking": u"严格错误检查",
    "Override Camera Markers": u"覆盖相机标记",
    "Blender Executable": u"Blender 可执行文件",
    "Applies to": u"应用于",
    "%d selected job(s)": u"已选中 %d 个作业",

    # Messages
    "Updated %d job(s).\n\nThe new options apply to the tasks that have not started yet; "
    "suspend and resume a running job (or enable 'Reload Plugin Between Tasks' for it) to "
    "have it pick them up immediately.":
        u"已更新 %d 个作业。\n\n新选项对尚未开始的任务生效；"
        u"要让正在运行的作业立刻生效，请挂起后恢复它（或为它启用 “Reload Plugin Between Tasks”）。",
    "%s\n\nThe drop-downs keep their current values. You can also set the names by hand in "
    "Job Properties -> Blender Settings.":
        u"%s\n\n下拉栏保持当前值。你也可以在 Job Properties -> Blender Settings 里手动填写这些名称。",
    "The .blend could not be read.": u"无法读取该 .blend 文件。",
    "The script stopped with an error:\n\n%s": u"脚本因错误中止：\n\n%s",
    "This script has to be run from the Monitor's Jobs panel:\n\n"
    "    right-click a job  ->  Scripts  ->  Blender Render Options\n\n"
    "It cannot run from the Launcher or from deadlinecommand (%s).":
        u"此脚本必须从 Monitor 的作业面板运行：\n\n"
        u"    右键作业  ->  Scripts  ->  Blender Render Options\n\n"
        u"它不能从 Launcher 或 deadlinecommand 运行（%s）。",
    "No job is selected.\n\nClick a job in the Jobs panel first - a right-click does not "
    "always select the row - and then run this script again.":
        u"没有选中任何作业。\n\n请先在作业面板里点击一个作业（右键并不总会选中该行），然后重新运行此脚本。",
    "These jobs do not use the Blender plugin and cannot be edited here:\n\n  %s":
        u"这些作业没有使用 Blender 插件，不能在这里编辑：\n\n  %s",
    "The scene '%s' is not in the submitted .blend file.": u"场景 “%s” 不在提交的 .blend 文件里。",
    "The view layer '%s' is not in scene '%s'.": u"渲染层 “%s” 不在场景 “%s” 里。",
    "The camera '%s' is not in the submitted .blend file.": u"相机 “%s” 不在提交的 .blend 文件里。",
    "Set both Resolution X and Resolution Y, or leave both at 0.":
        u"横向与纵向分辨率要么都设置，要么都留 0。",
    "The selected jobs do not all use the same .blend file, so the scene / view "
    "layer / camera names above cannot be valid for all of them. Select jobs that "
    "share a file, or clear those three fields before saving.":
        u"选中的作业并非都使用同一个 .blend 文件，因此上面的场景 / 渲染层 / 相机名称"
        u"不可能对它们全部有效。请选中使用同一文件的作业，或在保存前清空这三个字段。",
}

TRANSLATIONS = { "zh": TRANSLATIONS_ZH }


def SystemLanguage():
    # type: () -> str
    """The language of the operating system, e.g. zh-CN."""
    try:
        import clr
        from System.Globalization import CultureInfo

        name = str( CultureInfo.CurrentUICulture.Name )
        if name != "":
            return name
    except Exception:
        pass

    for variable in ( "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG" ):
        value = str( os.environ.get( variable, "" ) or "" ).strip()
        if value != "":
            return value

    return ""


def CurrentLanguage():
    # type: () -> str
    override = str( os.environ.get( LANGUAGE_ENVIRONMENT_VARIABLE, "" ) or "" ).strip()
    if override != "":
        return override

    return SystemLanguage()


def TranslationTable(language=None):
    # type: (str) -> dict
    name = CurrentLanguage() if language is None else str( language )
    if name == "":
        return {}

    # zh-CN, zh_HANS, zh_CN.UTF-8: all of them are Chinese.
    name = name.replace( "_", "-" ).split( "." )[ 0 ]
    code = name.split( "-" )[ 0 ].lower()
    return TRANSLATIONS.get( code, {} )


def Translate(text):
    # type: (str) -> str
    """The text in the language of the system, or unchanged when there is no translation."""
    table = TranslationTable()
    if not table:
        return text

    return table.get( text, text )


########################################################################
## The Read From File button and its states
########################################################################
# label, background, text colour. Pink while it waits, green when the names came out of the file,
# red when the read failed.
READ_BUTTON_STATES = (
    ( "ready", "READ FROM FILE", "#FFC0CB", "#000000" ),
    ( "done", "DONE", "#4CAF50", "#FFFFFF" ),
    ( "failed", "FAILED", "#D32F2F", "#FFFFFF" ),
)

readButtonWidget = None


def FindReadButton():
    """The Qt button behind the Read From File control, or None without Qt."""
    try:
        from PyQt5 import QtWidgets

        window = resolutionXSpin.window() if resolutionXSpin is not None else None
        if window is None:
            return None

        for button in window.findChildren( QtWidgets.QPushButton ):
            # The button is recognised by any of its labels: the English ones and their
            # translations, so a second lookup still finds it after the state changed.
            labels = set()
            for state, label, background, textColour in READ_BUTTON_STATES:
                labels.add( label.upper() )
                labels.add( Translate( label ).upper() )

            if button.text().strip().upper() in labels:
                return button
    except Exception:
        pass

    return None


def SetReadButtonState(state):
    """Show the button in one of its states: ready (pink), done (green), failed (red)."""
    global readButtonWidget

    if readButtonWidget is None:
        readButtonWidget = FindReadButton()
    if readButtonWidget is None:
        return

    for name, label, background, textColour in READ_BUTTON_STATES:
        if name != state:
            continue

        try:
            readButtonWidget.setText( Translate( label ) )
            readButtonWidget.setStyleSheet(
                "QPushButton { background-color: %s; color: %s; border: 1px solid #9E9E9E; "
                "border-radius: 3px; padding: 4px; }" % ( background, textColour ) )
        except Exception:
            pass
        return


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
        ShowInfo( Translate( "Blender Render Options - error" ),
                  Translate( "The script stopped with an error:\n\n%s" ) % traceback.format_exc())


def RunScript():
    global scriptDialog, selectedJobs, sceneBox

    Log("---- run requested")

    try:
        selectedJobs = list(MonitorUtils.GetSelectedJobs())
    except Exception as error:
        Log("GetSelectedJobs failed: %s" % error)
        ShowInfo( Translate( "Blender Render Options" ),
                  Translate( "This script has to be run from the Monitor's Jobs panel:\n\n"
                            "    right-click a job  ->  Scripts  ->  Blender Render Options\n\n"
                            "It cannot run from the Launcher or from deadlinecommand (%s)." ) % error)
        return

    Log("selected jobs: %d" % len(selectedJobs))
    if not selectedJobs:
        ShowInfo( Translate( "Blender Render Options" ),
                  Translate( "No job is selected.\n\nClick a job in the Jobs panel first - a right-click does not "
                            "always select the row - and then run this script again." ))
        return

    nonBlenderJobs = [job.JobName for job in selectedJobs if job.JobPlugin != "Blender"]
    if nonBlenderJobs:
        Log("not Blender jobs: %s" % ", ".join(nonBlenderJobs))
        ShowInfo( Translate( "Blender Render Options" ),
                  Translate( "These jobs do not use the Blender plugin and cannot be edited here:\n\n  %s" )
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

    grid = scriptDialog.AddGrid()
    row = 0

    # Read From File first, across the whole width of the window: it is the one step that may
    # have to touch the job's .blend file. The long explanation is the button's tooltip, because
    # a long label would decide the window's minimum width - and the window opens at its minimum.
    readButton = scriptDialog.AddControlToGrid(
        "ReadButton", "ButtonControl", Translate( "READ FROM FILE" ), row, 0,
        "Reads the scene, view layer and camera names out of the job's .blend file. "
        "It never loads the file: only its block index and structure catalog are read. "
        "Use it when this job has no recorded lists (a job submitted before the custom submitter, "
        "or from the Monitor).", True, colSpan=3)
    readButton.ValueModified.connect(ReadFromFilePressed)

    row += 1
    sceneBox = scriptDialog.AddComboControlToGrid(
        "SceneBox", "ComboControl", PluginValue(job, "RenderScene") or USE_SCENE_SETTING,
        tuple(NamesFor("scenes")), row, 1, expand=False)
    scriptDialog.AddControlToGrid("SceneLabel", "LabelControl", Translate( "Scene" ), row, 0,
                                  "The scene to render. The list comes from the submitted .blend file.", False)
    sceneBox.ValueModified.connect(SceneChanged)

    row += 1
    scriptDialog.AddControlToGrid("ViewLayerLabel", "LabelControl", Translate( "View Layer" ), row, 0,
                                  "The view layer to render; all other layers of the scene are disabled for the render.", False)
    scriptDialog.AddComboControlToGrid(
        "ViewLayerBox", "ComboControl", PluginValue(job, "ViewLayer") or USE_SCENE_SETTING,
        tuple(LayersForScene(PluginValue(job, "RenderScene") or USE_SCENE_SETTING)), row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("CameraLabel", "LabelControl", Translate( "Camera" ), row, 0,
                                  "The camera to render.", False)
    scriptDialog.AddComboControlToGrid(
        "CameraBox", "ComboControl", PluginValue(job, "Camera") or USE_SCENE_SETTING,
        tuple(NamesFor("cameras")), row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("EngineLabel", "LabelControl", Translate( "Render Engine" ), row, 0,
                                  "Overrides the render engine at render time.", False)
    scriptDialog.AddComboControlToGrid(
        "EngineBox", "ComboControl", PluginValue(job, "RenderEngine") or USE_SCENE_SETTING,
        ENGINE_ITEMS, row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("FormatLabel", "LabelControl", Translate( "Image Format" ), row, 0,
                                  "Overrides the output image format.", False)
    scriptDialog.AddComboControlToGrid(
        "FormatBox", "ComboControl", PluginValue(job, "ImageFormat") or USE_SCENE_SETTING,
        IMAGE_FORMAT_ITEMS, row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("GpuLabel", "LabelControl", Translate( "Cycles GPU Device" ), row, 0,
                                  "Cycles compute device: NONE forces CPU rendering. Only used by the Cycles engine.", False)
    scriptDialog.AddComboControlToGrid(
        "GpuBox", "ComboControl", PluginValue(job, "GpuDevice") or USE_SCENE_SETTING,
        GPU_DEVICE_ITEMS, row, 1, expand=False)

    row += 1
    scriptDialog.AddControlToGrid("ResolutionLabel", "LabelControl", Translate( "Resolution X / Y" ), row, 0,
                                  "Overrides the render resolution. 0 in both keeps the resolution stored in the .blend file.", False)
    # The two fields are one Qt cell so that they sit flush against each other; the dialog's grid
    # would leave a gap between two columns. Without Qt the two Deadline controls are used.
    if AddResolutionCell( grid, row, PluginValue(job, "ResolutionX", "0"), PluginValue(job, "ResolutionY", "0") ) == ( None, None ):
        scriptDialog.AddRangeControlToGrid(
            "ResolutionXBox", "RangeControl", int(PluginValue(job, "ResolutionX", "0") or 0),
            0, 65536, 0, 1, row, 1, expand=False)
        scriptDialog.AddRangeControlToGrid(
            "ResolutionYBox", "RangeControl", int(PluginValue(job, "ResolutionY", "0") or 0),
            0, 65536, 0, 1, row, 2, expand=False)

    row += 1
    scriptDialog.AddSelectionControlToGrid(
        "StrictErrorBox", "CheckBoxControl",
        bool(PluginValue(job, "StrictErrorChecking")), Translate( "Strict Error Checking" ), row, 1,
        "Fail the task when the render log matches the pattern configured in the Blender plugin settings.")

    row += 1
    scriptDialog.AddSelectionControlToGrid(
        "MarkerOverrideBox", "CheckBoxControl",
        bool(PluginValue(job, "MarkerOverride")), Translate( "Override Camera Markers" ), row, 1,
        "When a specific camera is selected, also rebind every timeline marker to it.")

    row += 1
    scriptDialog.AddControlToGrid("BlenderExeLabel", "LabelControl", Translate( "Blender Executable" ), row, 0,
                                  "Only used for ZStandard-compressed .blend files when no zstd module is available; "
                                  "reading the names normally does not start Blender at all.", False)
    scriptDialog.AddSelectionControlToGrid(
        "BlenderExeBox", "FileBrowserControl", DefaultBlenderExecutable(),
        "Blender (blender.exe blender);;All Files (*)", row, 1, colSpan=2)

    row += 1
    scriptDialog.AddControlToGrid("JobsLabel", "LabelControl", Translate( "Applies to" ), row, 0,
                                  "The options are written to every selected Blender job.", False)
    scriptDialog.AddControlToGrid("JobsBox", "LabelControl",
                                  Translate( "%d selected job(s)" ) % len(selectedJobs), row, 1, "", False)

    saveButton = scriptDialog.AddControlToGrid("SaveButton", "ButtonControl", Translate( "Save" ), row + 1, 1, expand=False)
    saveButton.ValueModified.connect(SavePressed)
    closeButton = scriptDialog.AddControlToGrid("CloseButton", "ButtonControl", Translate( "Close" ), row + 1, 2, expand=False)
    closeButton.ValueModified.connect(ClosePressed)
    scriptDialog.EndGrid()

    # The wide Read button is made a little taller than a normal one and put in its waiting state
    # (pink); the window opens at the smallest size it can be dragged to - the layout decides both.
    SetReadButtonState("ready")

    try:
        from PyQt5 import QtWidgets

        window = resolutionXSpin.window() if resolutionXSpin is not None else None
        if window is not None:
            if readButtonWidget is not None:
                readButtonWidget.setMinimumHeight( 34 )

            minimum = window.minimumSizeHint()
            scriptDialog.SetSize( minimum.width(), minimum.height() )
            Log("dialog opens at its minimum size %dx%d" % ( minimum.width(), minimum.height() ))
    except Exception as error:
        Log("could not take the minimum size: %s" % error)

    scriptDialog.ShowDialog(False)
    Log("dialog shown")


def ReadFromFilePressed(*args):
    """The one step that touches the network: read the names out of the .blend."""
    global readError

    SetReadButtonState("ready")
    Log("reading %s ..." % (JobBlendPath(selectedJobs[0]) or "?"))

    if not ReadNamesFromBlendFile():
        SetReadButtonState("failed")
        Log("read failed: %s" % (readError or "unknown"))
        scriptDialog.ShowMessageBox(
            Translate( "%s\n\nThe drop-downs keep their current values. You can also set the names by hand in "
                       "Job Properties -> Blender Settings." )
            % (readError or Translate( "The .blend could not be read." )),
            Translate( "Blender Render Options" ))
        return

    SetReadButtonState("done")
    # No status line in the dialog: what was read goes to the log, and the drop-downs show it.
    Log("read result: %s" % StatusText())
    Log(StatusTooltip())
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
            return Translate( "The scene '%s' is not in the submitted .blend file." ) % scene
        if layer not in (USE_SCENE_SETTING, ""):
            layers = blendNames["scenes"].get(scene, [])
            if layers and layer not in layers:
                return Translate( "The view layer '%s' is not in scene '%s'." ) % (layer, scene)
        if camera not in (USE_SCENE_SETTING, "") and camera not in blendNames["cameras"]:
            return Translate( "The camera '%s' is not in the submitted .blend file." ) % camera

    resolutionX, resolutionY = ResolutionValues()
    if (resolutionX > 0) != (resolutionY > 0):
        return Translate( "Set both Resolution X and Resolution Y, or leave both at 0." )

    if len(selectedJobs) > 1:
        files = set(JobBlendPath(job) for job in selectedJobs)
        if len(files) > 1 and blendNames:
            return Translate( "The selected jobs do not all use the same .blend file, so the scene / view "
                              "layer / camera names above cannot be valid for all of them. Select jobs that "
                              "share a file, or clear those three fields before saving." )

    return ""


def SavePressed(*args):
    global scriptDialog

    problem = Validate()
    if problem:
        scriptDialog.ShowMessageBox(problem, Translate( "Blender Render Options" ))
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

            if ResolutionOverridden():
                for pluginInfoKey, value in ( ( "ResolutionX", ResolutionValues()[ 0 ] ),
                                              ( "ResolutionY", ResolutionValues()[ 1 ] ) ):
                    job.SetJobPluginInfoKeyValue(pluginInfoKey, str(value) if value > 0 else "")
            else:
                for controlName, pluginInfoKey in NUMBER_KEYS:
                    value = int(scriptDialog.GetValue(controlName))
                    job.SetJobPluginInfoKeyValue(pluginInfoKey, str(value) if value > 0 else "")

            for controlName, pluginInfoKey in CHECK_KEYS:
                job.SetJobPluginInfoKeyValue(
                    pluginInfoKey, "True" if scriptDialog.GetValue(controlName) else "")

            RepositoryUtils.SaveJob(job)

        scriptDialog.ShowMessageBox(
            Translate( "Updated %d job(s).\n\nThe new options apply to the tasks that have not started yet; "
                       "suspend and resume a running job (or enable 'Reload Plugin Between Tasks' for it) to "
                       "have it pick them up immediately." ) % len(selectedJobs),
            Translate( "Blender Render Options" ))
    except Exception:
        scriptDialog.ShowMessageBox(traceback.format_exc(), Translate( "Blender Render Options" ))


def ClosePressed(*args):
    global scriptDialog
    scriptDialog.CloseDialog()
