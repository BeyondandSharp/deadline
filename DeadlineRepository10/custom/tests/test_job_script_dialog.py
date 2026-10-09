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

    module.DeadlineScriptDialog = RecordingDialog
    module.__main__()
    return shown, info, requested, saved


def __main__():
    print("job script: %s" % JOB_SCRIPT)

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
        print("Save really writes the chosen values into the job")
        module.scriptDialog.SetValue("SceneBox", "Scene_Main")
        module.scriptDialog.SetValue("ViewLayerBox", "Beauty")
        module.scriptDialog.SetValue("CameraBox", "ShotCam_0")
        module.scriptDialog.SetValue("EngineBox", "cycles")
        module.scriptDialog.SetValue("FormatBox", "PNG")
        module.scriptDialog.SetValue("GpuBox", "OPTIX")
        module.scriptDialog.SetValue("ResolutionXBox", 1920)
        module.scriptDialog.SetValue("ResolutionYBox", 1080)
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
            reader = sys.modules.get("blend_names")
            check(reader is not None and all(name in requested for name in reader.repository_files()),
                  "the reader and all %d parser files were requested through the repository API"
                  % (len(reader.repository_files()) if reader else 0))
        else:
            print("    (skipped, no Blender)")

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
