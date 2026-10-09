"""Checks for custom/scripts/Jobs/blender_render_options.py that need no running Deadline.

The job script is loaded with stubbed Deadline modules, so this catches import-time errors
and - more usefully - **key drift**: every plugin-info key the dialog writes has to be

  * declared in custom/plugins/Blender/Blender.options (otherwise it is invisible in the
    Monitor's Job Properties page), and
  * actually read by custom/plugins/Blender/Blender.py (otherwise the dialog writes into
    the void, which is exactly the class of bug that made the render engine setting do
    nothing earlier).

Run:

    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_job_script.py
"""

import importlib.util
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
CUSTOM = os.path.normpath(os.path.join(HERE, ".."))
JOB_SCRIPT = os.path.join(CUSTOM, "scripts", "Jobs", "blender_render_options.py")
OPTIONS_FILE = os.path.join(CUSTOM, "plugins", "Blender", "Blender.options")
PLUGIN_FILE = os.path.join(CUSTOM, "plugins", "Blender", "Blender.py")
RENDER_SCRIPT_FILE = os.path.join(CUSTOM, "plugins", "Blender", "BlenderRenderScript.py")
SUBMISSION_FILE = os.path.join(CUSTOM, "scripts", "Submission", "BlenderSubmission.py")

FAILURES = []


def check(condition, message):
    if condition:
        print("    OK   %s" % message)
    else:
        FAILURES.append(message)
        print("    FAIL %s" % message)


class FakeDialog:
    """Stands in for DeadlineScriptDialog: only the calls the job script makes."""

    def __init__(self, values=None):
        self.values = values or {}
        self.messages = []

    def GetValue(self, name):
        return self.values.get(name, "")

    def ShowMessageBox(self, message, title=""):
        self.messages.append((title, message))


def load_job_script():
    """Import the job script with stub Deadline modules in place."""
    deadline = types.ModuleType("Deadline")
    scripting = types.ModuleType("Deadline.Scripting")
    scripting.MonitorUtils = types.SimpleNamespace(GetSelectedJobs=lambda: [])
    scripting.RepositoryUtils = types.SimpleNamespace(
        # Resolve a repository-relative path onto the working copy, like the real cache does.
        GetRepositoryFilePath=lambda path, custom=False: os.path.join(CUSTOM, path.replace("/", os.sep)),
        GetJobAuxiliaryPath=lambda job: tempfile.gettempdir(),
    )
    scripting.ClientUtils = types.SimpleNamespace(
        GetUsersSettingsDirectory=lambda: tempfile.gettempdir())
    deadline.Scripting = scripting

    ui = types.ModuleType("DeadlineUI")
    controls = types.ModuleType("DeadlineUI.Controls")
    scripting_controls = types.ModuleType("DeadlineUI.Controls.Scripting")
    dialog_module = types.ModuleType("DeadlineUI.Controls.Scripting.DeadlineScriptDialog")
    dialog_module.DeadlineScriptDialog = FakeDialog
    for name, module in (
        ("Deadline", deadline),
        ("Deadline.Scripting", scripting),
        ("DeadlineUI", ui),
        ("DeadlineUI.Controls", controls),
        ("DeadlineUI.Controls.Scripting", scripting_controls),
        ("DeadlineUI.Controls.Scripting.DeadlineScriptDialog", dialog_module),
    ):
        sys.modules[name] = module

    spec = importlib.util.spec_from_file_location("blender_render_options", JOB_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def options_sections():
    with open(OPTIONS_FILE, "r", encoding="utf-8") as handle:
        return set(re.findall(r"^\[([^\]]+)\]", handle.read(), re.MULTILINE))


def submission_plugin_info_keys():
    """Plugin info keys the submission dialog writes, taken from the plugin info writer."""
    with open(SUBMISSION_FILE, "r", encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("def WritePluginInfoFile(")
    end = source.index("def SubmitJobs(", start)
    block = source[start:end]

    keys = set(re.findall(r'"([A-Za-z][A-Za-z0-9]*)="', block))          # "Key=" + value
    keys |= set(re.findall(r'"([A-Za-z][A-Za-z0-9]*)=%', block))          # "Key=%s" % value
    keys |= set(re.findall(r'\(\s*"([A-Za-z][A-Za-z0-9]*)"\s*,\s*(?:values|job)(?:\.get\(|\[)', block))
    return keys


def control_names(source):
    """Names of the controls the dialog creates, and the ones the code reads or writes."""
    created = set(re.findall(r'Add[A-Za-z]*ToGrid\(\s*"([A-Za-z0-9_]+)"', source))
    used = set(re.findall(r'scriptDialog\.(?:GetValue|SetValue|SetItems|SetEnabled|SetVisible)\(\s*"([A-Za-z0-9_]+)"', source))
    return created, used


def main():
    print("job script: %s" % JOB_SCRIPT)
    module = load_job_script()
    print("    OK   imported with stubbed Deadline modules")

    print("")
    print("custom/lib is found through the repository path")
    lib = module.LibDirectory()
    check(os.path.isfile(os.path.join(lib, "blend_names.py")),
          "LibDirectory() -> %s (has blend_names.py)" % lib)

    print("")
    print("every key the dialog writes is declared and read")
    sections = options_sections()
    with open(PLUGIN_FILE, "r", encoding="utf-8") as handle:
        plugin_source = handle.read()
    with open(RENDER_SCRIPT_FILE, "r", encoding="utf-8") as handle:
        render_source = handle.read()

    managed = list(module.TEXT_KEYS) + list(module.NUMBER_KEYS) + list(module.CHECK_KEYS)
    for control, key in managed:
        check(key in sections, "%s is declared in Blender.options" % key)
        check('"%s"' % key in plugin_source, "%s is read by Blender.py" % key)

    # The three name fields are handled by the render script as well.
    for key in ("RenderScene", "ViewLayer", "Camera"):
        check(key in plugin_source, "%s is passed on by Blender.py" % key)

    # The dialog's own drop-down values must match what the option files list, otherwise a
    # saved value would not be selectable in Job Properties.
    with open(OPTIONS_FILE, "r", encoding="utf-8") as handle:
        options_source = handle.read()
    for section, items in (
        ("RenderEngine", module.ENGINE_ITEMS),
        ("ImageFormat", module.IMAGE_FORMAT_ITEMS),
        ("GpuDevice", module.GPU_DEVICE_ITEMS),
    ):
        match = re.search(r"^\[%s\](.*?)(?=^\[|\Z)" % section, options_source,
                          re.MULTILINE | re.DOTALL)
        declared = set()
        if match:
            values = re.search(r"^Values=(.*)$", match.group(1), re.MULTILINE)
            if values:
                declared = set(values.group(1).strip().split(";"))
        check(set(items) == declared,
              "%s drop-down matches Blender.options (%d values)" % (section, len(items)))

    # The render script has to understand each option the plugin sends.
    for token in ("engine", "scene", "layer", "camera", "format", "resx", "resy", "gpu",
                  "markers", "frames", "output", "threads", "render"):
        check('"%s"' % token in render_source, "render script handles the '%s' option" % token)

    print("")
    print("every control the dialog touches exists")
    with open(JOB_SCRIPT, "r", encoding="utf-8") as handle:
        script_source = handle.read()
    created, used = control_names(script_source)
    check(len(created) >= 15, "found %d controls created by the dialog" % len(created))
    for control, key in managed:
        check(control in created, "%s (writes %s) is created by BuildDialog" % (control, key))
    check("scriptDialog.GetValue(controlName)" in script_source,
          "SavePressed reads the controls through the tables, so those names must exist")
    for control in sorted(used):
        check(control in created,
              "%s is read with GetValue/SetValue but never created" % control)

    print("")
    print("the submission dialog writes the same keys")
    submissionKeys = submission_plugin_info_keys()
    check(len(submissionKeys) >= 12,
          "found %d statically written plugin info keys in BlenderSubmission.py" % len(submissionKeys))
    for key in sorted(submissionKeys):
        check(key in sections, "%s is declared in Blender.options" % key)
    # Written from a loop, so the text scan above cannot see them.
    for key in ("AvailableScenes", "AvailableViewLayers", "AvailableCameras"):
        check(key in sections, "%s (written dynamically) is declared in Blender.options" % key)

    print("")
    print("validation")
    module.blendNames = {"scenes": {"Scene_Main": ["ViewLayer", "Beauty"]}, "cameras": ["Camera"]}
    module.selectedJobs = [object()]

    module.scriptDialog = FakeDialog({
        "SceneBox": "Scene_Main", "ViewLayerBox": "Beauty", "CameraBox": "Camera",
        "ResolutionXBox": 0, "ResolutionYBox": 0,
    })
    check(module.Validate() == "", "a consistent selection passes")

    module.scriptDialog = FakeDialog({
        "SceneBox": "Nope", "ViewLayerBox": "Beauty", "CameraBox": "Camera",
        "ResolutionXBox": 0, "ResolutionYBox": 0,
    })
    check("not in the submitted" in module.Validate(), "an unknown scene is rejected")

    module.scriptDialog = FakeDialog({
        "SceneBox": "Scene_Main", "ViewLayerBox": "Denoise", "CameraBox": "Camera",
        "ResolutionXBox": 0, "ResolutionYBox": 0,
    })
    check("is not in scene" in module.Validate(), "a view layer of another scene is rejected")

    module.scriptDialog = FakeDialog({
        "SceneBox": "Scene_Main", "ViewLayerBox": "Beauty", "CameraBox": "Camera",
        "ResolutionXBox": 1920, "ResolutionYBox": 0,
    })
    check("both Resolution" in module.Validate(), "half a resolution is rejected")

    print("")
    if FAILURES:
        print("RESULT: %d check(s) failed" % len(FAILURES))
        for failure in FAILURES:
            print("  - %s" % failure)
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
