"""Build the "Blender Render Options" job dialog for real, without showing it.

Runs under Deadline's own Python (deadlinecommand), so it exercises the actual
DeadlineScriptDialog control API - which a plain Python test cannot do. The selected jobs,
RepositoryUtils and ShowDialog are stubbed; everything else is the real code.

    deadlinecommand -ExecuteScript "<DeadlineRepository>\\custom\\tests\\test_job_script_dialog.py"

What it guards, in order of importance:

  * the dialog is built and shown **without touching the .blend** (a job's file can live on a
    share that takes minutes to answer - that is what made the dialog never appear),
  * the lists recorded in the job are used when they exist,
  * "Read From File" fills the drop-downs from the .blend,
  * and every entry path reports something instead of ending in silence: nothing selected, a
    non-Blender job, GetSelectedJobs() raising (Launcher / deadlinecommand), and the dialog
    construction raising.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CUSTOM = os.path.normpath(os.path.join(HERE, ".."))
JOB_SCRIPT = os.path.join(CUSTOM, "scripts", "Jobs", "blender_render_options.py")

BLENDER_CANDIDATES = (
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
)

MAKE = r'''
import bpy
for index in range(2):
    cam_data = bpy.data.cameras.new("CamData_%d" % index)
    cam_obj = bpy.data.objects.new("ShotCam_%d" % index, cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
main = bpy.context.scene
main.name = "Scene_Main"
main.view_layers.new("Beauty")
extra = bpy.data.scenes.new("Scene_Alt")
extra.view_layers.new("Layers_A")
bpy.ops.wm.save_as_mainfile(filepath=r"__BLEND__", compress=False)
'''

FAILURES = []


def check(condition, message):
    if condition:
        print("    OK   %s" % message)
    else:
        FAILURES.append(message)
        print("    FAIL %s" % message)


def make_blend(path):
    blender = next((candidate for candidate in BLENDER_CANDIDATES if os.path.isfile(candidate)), None)
    if blender is None:
        return None

    script = os.path.join(tempfile.gettempdir(), "dlb_make_scene.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(MAKE.replace("__BLEND__", path))
    subprocess.run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", script],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    os.unlink(script)
    return path if os.path.isfile(path) else None


class FakeJob(object):
    """Only the members the real Deadline Job class has, so a wrong attribute name fails.

    A fake that offers more than the real API hides bugs: the earlier version of the script
    asked for ``job.JobPluginName``, which does not exist (it is ``job.JobPlugin``), and this
    fake happily provided it.
    """

    JobName = "FakeBlenderJob"
    JobPlugin = "Blender"
    JobAuxiliarySubmissionFileNames = []

    def __init__(self, plugin="Blender", values=None):
        self.JobPlugin = plugin
        self.JobName = "FakeJob-%s" % plugin
        self.values = values or {}

    def GetJobPluginInfoKeyValue(self, key):
        return self.values.get(key, "")

    def SetJobPluginInfoKeyValue(self, key, value):
        self.values[key] = value


class HostileJob(object):
    """A job whose properties blow up, to prove nothing escapes __main__ silently."""

    @property
    def JobName(self):
        raise RuntimeError("simulated job property failure")

    @property
    def JobPlugin(self):
        raise RuntimeError("simulated job property failure")


def load_module():
    spec = importlib.util.spec_from_file_location("blender_render_options", JOB_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_case(module, label, get_jobs, work, dialog_class=None):
    """Run __main__ once and report (dialogs shown, info dialogs, repository paths, saved jobs)."""
    shown = []
    info = []
    requested = []
    saved = []

    def repository_file(relative, custom=False):
        requested.append(relative)
        return os.path.join(CUSTOM, relative.replace("/", os.sep))

    module.MonitorUtils = types.SimpleNamespace(GetSelectedJobs=get_jobs)
    module.RepositoryUtils = types.SimpleNamespace(
        GetRepositoryFilePath=repository_file,
        GetJobAuxiliaryPath=lambda job: work,
        SaveJob=lambda job: saved.append(job),
    )
    module.ShowInfo = lambda title, text: info.append((title, text))

    real_dialog = module.DeadlineScriptDialog
    base = real_dialog if dialog_class is None else dialog_class

    class RecordingDialog(base):
        def ShowDialog(self, *args, **kwargs):
            shown.append(label)
            return True

        def ShowMessageBox(self, message, title=""):
            info.append((title, message))
            return True

        def SetSize(self, width, height):
            # The dialog opens at its minimum size, so record what it asked for.
            module.scriptDialogLastSize = (width, height)
            return base.SetSize(self, width, height)

    module.DeadlineScriptDialog = RecordingDialog
    module.__main__()
    return shown, info, requested, saved


def __main__():
    print("job script: %s" % JOB_SCRIPT)

    # The dialog follows the language of the operating system. Pin it here, so the assertions
    # below do not depend on the machine; the Chinese case is checked further down.
    os.environ["DLB_LANGUAGE"] = "en_US"

    work = tempfile.mkdtemp(prefix="dlb_dialog_test_")
    blend = os.path.join(work, "scene.blend")
    try:
        have_blend = make_blend(blend) is not None
        if not have_blend:
            print("no Blender found to create a test scene; the file-read checks will be skipped")

        recorded = FakeJob(values={
            "SceneFile": blend,
            "AvailableScenes": "Scene_Main, Scene_Alt",
            "AvailableViewLayers": "ViewLayer, Beauty, Layers_A",
            "AvailableCameras": "Camera, ShotCam_0",
        })

        print("")
        print("a job with recorded name lists: the dialog must not touch the file")
        module = load_module()
        shown, info, requested, saved = run_case(module, "main", lambda: [recorded], work)
        check(bool(shown), "the dialog is shown (ShowDialog was called)")
        check(not info, "no error dialog instead")
        check(module.blendPath == "", "no .blend was looked at (blendPath is still empty)")
        check(bool(module.blendNames) and module.blendNames["source"].startswith("the job"),
              "the drop-downs come from the job's recorded lists")
        check([name for name in module.NamesFor("scenes") if name != "Use Scene Setting"]
              == ["Scene_Alt", "Scene_Main"],
              "the scene list: %s" % module.NamesFor("scenes"))

        print("")
        print("the layout: Read From File on top, markers under strict error checking")
        from PyQt5 import QtWidgets

        def GridPlace(widget):
            layout = widget.window().findChild(QtWidgets.QGridLayout)
            index = layout.indexOf(widget)
            if index < 0:
                return None
            row, column, rowSpan, columnSpan = layout.getItemPosition(index)
            return row, column

        checkBoxes = dict((box.text(), box)
                          for box in module.resolutionXSpin.window().findChildren(QtWidgets.QCheckBox)
                          if box.text() in ("Strict Error Checking", "Override Camera Markers"))
        check(len(checkBoxes) == 2, "both check boxes are in the dialog: %s" % sorted(checkBoxes))
        if len(checkBoxes) == 2:
            strictPlace = GridPlace(checkBoxes["Strict Error Checking"])
            markerPlace = GridPlace(checkBoxes["Override Camera Markers"])
            check(strictPlace is not None and markerPlace is not None,
                  "their place in the grid is known: %s / %s" % (strictPlace, markerPlace))
            if strictPlace and markerPlace:
                check(markerPlace[0] > strictPlace[0],
                      "Override Camera Markers is below Strict Error Checking: %s -> %s"
                      % (strictPlace, markerPlace))
                check(markerPlace[1] == strictPlace[1],
                      "and in the same column: %s -> %s" % (strictPlace, markerPlace))

        buttons = dict((button.text().strip(), button)
                       for button in module.resolutionXSpin.window().findChildren(QtWidgets.QPushButton))
        check("READ FROM FILE" in buttons, "the Read From File button is there: %s" % sorted(buttons))
        if "READ FROM FILE" in buttons:
            # Waiting state: pink.
            style = buttons["READ FROM FILE"].styleSheet().lower()
            check("#ffc0cb" in style,
                  "it waits in pink: %r" % buttons["READ FROM FILE"].styleSheet())
        if "READ FROM FILE" in buttons:
            readButton = buttons["READ FROM FILE"]
            check(readButton.minimumHeight() >= 30,
                  "it is taller than a normal button (%d px)" % readButton.minimumHeight())
            readPlace = GridPlace(readButton)
            check(readPlace is not None and readPlace[1] == 0,
                  "and starts in the first column: %s" % (readPlace,))

        combos = module.resolutionXSpin.window().findChildren(QtWidgets.QComboBox)
        if "READ FROM FILE" in buttons and combos:
            readPlace = GridPlace(buttons["READ FROM FILE"])
            comboRows = [GridPlace(combo)[0] for combo in combos if GridPlace(combo)]
            check(readPlace is not None and comboRows and readPlace[0] < min(comboRows),
                  "it sits above every drop-down: %s < %s" % (readPlace, comboRows))

        # The window opens at the smallest size it can be dragged to.
        check(module.scriptDialogLastSize is not None,
              "the dialog asked for a size: %s" % (module.scriptDialogLastSize,))
        if module.scriptDialogLastSize is not None:
            window = module.resolutionXSpin.window()
            minimum = window.minimumSizeHint()
            check(tuple(module.scriptDialogLastSize) == (minimum.width(), minimum.height()),
                  "and it is the minimum size (%s vs %dx%d)"
                  % (module.scriptDialogLastSize, minimum.width(), minimum.height()))

        layout = module.resolutionXSpin.window().findChild(QtWidgets.QGridLayout)
        readIndex = layout.indexOf(buttons["READ FROM FILE"])
        if readIndex >= 0:
            row, column, rowSpan, columnSpan = layout.getItemPosition(readIndex)
            check(columnSpan == 3,
                  "the button spans the whole width of the grid (%d columns)" % columnSpan)

        for removed in ("ReadHintLabel", "StatusBox", "StatusLabel"):
            try:
                module.scriptDialog.GetValue(removed)
                check(False, "%s is gone" % removed)
            except Exception:
                check(True, "%s is gone" % removed)

        resolutionX = module.resolutionXSpin
        resolutionY = module.resolutionYSpin
        check(resolutionX is not None and resolutionY is not None,
              "the resolution is a Qt pair")
        if resolutionX is not None and resolutionY is not None:
            check(resolutionX.parent() is resolutionY.parent(),
                  "both fields share one cell")
            check(resolutionX.parent().layout().spacing() == 0,
                  "and sit flush against each other (spacing %d)"
                  % resolutionX.parent().layout().spacing())

        print("")
        print("Save really writes the chosen values into the job")
        module.scriptDialog.SetValue("SceneBox", "Scene_Main")
        module.scriptDialog.SetValue("ViewLayerBox", "Beauty")
        module.scriptDialog.SetValue("CameraBox", "ShotCam_0")
        module.scriptDialog.SetValue("EngineBox", "cycles")
        module.scriptDialog.SetValue("FormatBox", "PNG")
        module.scriptDialog.SetValue("GpuBox", "OPTIX")
        # The resolution is a Qt cell now, so its two spin boxes are set directly.
        module.resolutionXSpin.setValue(1920)
        module.resolutionYSpin.setValue(1080)
        module.scriptDialog.SetValue("StrictErrorBox", True)
        module.scriptDialog.SetValue("MarkerOverrideBox", False)
        info.clear()
        module.SavePressed()
        message = info[0][1] if info else ""
        check("Traceback" not in message and "KeyError" not in message and "AttributeError" not in message,
              "Save completed without an error (%s)"
              % (message.splitlines()[0] if message else "no message"))
        check("Updated" in message, "the dialog confirms the save (%s)"
              % (message.splitlines()[0] if message else "-"))
        check(len(saved) == 1, "the job was saved (%d)" % len(saved))
        written = recorded.values
        for key, expected in (("RenderScene", "Scene_Main"), ("ViewLayer", "Beauty"),
                              ("Camera", "ShotCam_0"), ("RenderEngine", "cycles"),
                              ("ImageFormat", "PNG"), ("GpuDevice", "OPTIX"),
                              ("ResolutionX", "1920"), ("ResolutionY", "1080"),
                              ("StrictErrorChecking", "True"), ("MarkerOverride", "")):
            check(written.get(key) == expected,
                  "%s = %r (expected %r)" % (key, written.get(key), expected))

        print("")
        print("Read From File fills the drop-downs from the .blend")
        if have_blend:
            module = load_module()
            read_job = FakeJob(values={"SceneFile": blend})
            shown, info, requested, saved = run_case(module, "main", lambda: [read_job], work)
            check(not module.blendNames, "no lists before pressing the button")
            module.ReadFromFilePressed()
            check(bool(module.blendNames), "the file was read")
            if module.blendNames:
                check("parser" in module.blendNames["source"],
                      "read by %s in %.3fs" % (module.blendNames["source"], module.blendNames["seconds"]))
                check(sorted(module.blendNames["scenes"]) == ["Scene_Alt", "Scene_Main"],
                      "scenes found: %s" % sorted(module.blendNames["scenes"]))
                check("ShotCam_0" in module.NamesFor("cameras"),
                      "cameras found: %s" % module.NamesFor("cameras"))
            check(not info, "no error dialog")

            doneButton = module.readButtonWidget
            check(doneButton is not None and doneButton.text().strip() == "DONE",
                  "the button reports DONE: %r" % (doneButton.text() if doneButton else None))
            if doneButton is not None:
                check("#4caf50" in doneButton.styleSheet().lower(),
                      "and turns green: %r" % doneButton.styleSheet())

            reader = sys.modules.get("blend_names")
            check(reader is not None and all(name in requested for name in reader.repository_files()),
                  "the reader and all %d parser files were requested through the repository API"
                  % (len(reader.repository_files()) if reader else 0))
        else:
            print("    (skipped, no Blender)")

        print("")
        print("a failed read shows FAILED in red")
        brokenPath = os.path.join(work, "broken.blend")
        with open(brokenPath, "w", encoding="utf-8") as handle:
            handle.write("this is not a .blend file")
        module = load_module()
        brokenJob = FakeJob(values={"SceneFile": brokenPath})
        shown, info, requested, saved = run_case(module, "main", lambda: [brokenJob], work)
        module.ReadFromFilePressed()
        check(bool(info), "the failure is reported in a dialog: %s" % [title for title, _ in info])
        failedButton = module.readButtonWidget
        check(failedButton is not None and failedButton.text().strip() == "FAILED",
              "the button reports FAILED: %r" % (failedButton.text() if failedButton else None))
        if failedButton is not None:
            check("#d32f2f" in failedButton.styleSheet().lower(),
                  "and turns red: %r" % failedButton.styleSheet())

        print("")
        print("the dialog follows the language of the system")
        module = load_module()
        check(module.TranslationTable("en_US") == {},
              "English means no translation table")
        check(module.TranslationTable("zh-CN") is module.TRANSLATIONS_ZH,
              "a system language of zh-CN selects the Chinese table")
        check(module.TranslationTable("zh_HANS") is module.TRANSLATIONS_ZH,
              "and so does zh_HANS")
        check(module.Translate("Scene") == "Scene", "English keeps the English label")

        os.environ["DLB_LANGUAGE"] = "zh_HANS"
        try:
            check(module.Translate("Scene") == u"\u573a\u666f",
                  "Chinese translates a label: %r" % module.Translate("Scene"))
            check(module.Translate("READ FROM FILE") == u"\u4ece\u6587\u4ef6\u8bfb\u53d6",
                  "and the button: %r" % module.Translate("READ FROM FILE"))
            check(module.Translate("Override Camera Markers") == u"\u8986\u76d6\u76f8\u673a\u6807\u8bb0",
                  "and the check boxes: %r" % module.Translate("Override Camera Markers"))
            check(module.Translate("Not translated at all") == "Not translated at all",
                  "an unknown string is returned unchanged")

            chineseModule = load_module()
            shown, info, requested, saved = run_case(chineseModule, "main", lambda: [FakeJob(values={"SceneFile": blend})], work)
            chineseButtons = [button.text().strip() for button in
                              chineseModule.resolutionXSpin.window().findChildren(QtWidgets.QPushButton)]
            check(u"\u4ece\u6587\u4ef6\u8bfb\u53d6" in chineseButtons,
                  "the dialog itself is Chinese: %s" % chineseButtons)
            chineseLabels = [label.text() for label in
                             chineseModule.resolutionXSpin.window().findChildren(QtWidgets.QLabel)]
            check(u"\u573a\u666f" in chineseLabels,
                  "its labels too: %s" % chineseLabels)
        finally:
            os.environ["DLB_LANGUAGE"] = "en_US"

        print("")
        print("nothing selected")
        module = load_module()
        shown, info, requested, saved = run_case(module, "main", lambda: [], work)
        check(bool(info) and "No job is selected" in info[0][1],
              "an explaining dialog is shown instead of nothing at all")
        check(not shown, "no half-built dialog is shown")

        print("")
        print("a job that is not a Blender job")
        module = load_module()
        shown, info, requested, saved = run_case(module, "main", lambda: [FakeJob(plugin="MayaBatch")], work)
        check(bool(info) and "do not use the Blender plugin" in info[0][1],
              "an explaining dialog is shown")

        print("")
        print("GetSelectedJobs() is not available (Launcher / deadlinecommand)")
        module = load_module()

        def boom():
            raise RuntimeError("MonitorUtils.GetSelectedJobs is only valid when a script is "
                               "being executed from the Monitor")

        shown, info, requested, saved = run_case(module, "main", boom, work)
        check(bool(info) and "run from the Monitor's Jobs panel" in info[0][1],
              "an explaining dialog is shown")

        print("")
        print("a job whose properties raise must not escape silently")
        module = load_module()
        shown, info, requested, saved = run_case(module, "main", lambda: [HostileJob()], work)
        check(bool(info) and "simulated job property failure" in info[0][1],
              "the error is reported in a dialog instead of only in the Monitor's error output")

        print("")
        print("building the dialog raises")
        module = load_module()

        class ExplodingDialog(object):
            def __init__(self, *args, **kwargs):
                raise RuntimeError("simulated dialog failure")

        shown, info, requested, saved = run_case(module, "main", lambda: [recorded], work, dialog_class=ExplodingDialog)
        check(bool(info) and "simulated dialog failure" in info[0][1],
              "an error dialog with the traceback is shown")

        print("")
        if FAILURES:
            print("RESULT: %d check(s) failed" % len(FAILURES))
            for failure in FAILURES:
                print("  - %s" % failure)
            return 1
        print("RESULT: dialog opens without file I/O and every entry path reports something")
        return 0
    except Exception:
        print("RESULT: FAILED - exception in the test itself:")
        print(traceback.format_exc())
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(__main__())
