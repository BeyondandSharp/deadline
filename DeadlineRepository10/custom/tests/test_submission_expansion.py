"""Tests for the submission side of custom/scripts/Submission/BlenderSubmission.py.

Runs with the plain venv Python: the submission script is loaded with stubbed Deadline /
.NET modules, and only the functions that build and submit the job files are exercised.
The Qt parts (the combination tree and the per-combination options table) are tested by
tests/test_submission_dialog.py, which runs under Deadline.

    <repo>\\.venv\\Scripts\\python.exe custom\\tests\\test_submission_expansion.py

Covered here:
  * SanitizeToken / UniqueTokens      - safe, collision-free name and file name parts
  * CombinationsFromPaths             - the ticked leaves, without duplicates
  * BuildJobs                          - job names, output paths, the job cap, error messages
  * CollectValues + WriteJobFiles + SubmitJobs with a stub dialog:
      - one combination keeps the plain job name and the untouched output path
      - several combinations get a name suffix, their own output file and a BatchName
      - every combination's own options (engine, format, GPU, resolution, switches) end up in
        that job's plugin info file
      - the submission uses -SubmitMultipleJobs with one -Job group per combination
  * every control the script reads is one the dialog creates (the check that catches
    "GetValue on a control that does not exist" - a bug this codebase already had once)
"""

import importlib.util
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
CUSTOM = os.path.normpath(os.path.join(HERE, ".."))
SUBMISSION_SCRIPT = os.path.join(CUSTOM, "scripts", "Submission", "BlenderSubmission.py")

FAILURES = []


def check(condition, message):
    if condition:
        print("    OK   %s" % message)
    else:
        FAILURES.append(message)
        print("    FAIL %s" % message)


########################################################################
## Stubs
########################################################################
class FakePath(object):
    """System.IO.Path, with the Windows semantics the script relies on."""

    @staticmethod
    def Combine(*parts):
        return os.path.join(*parts)

    @staticmethod
    def GetDirectoryName(path):
        return os.path.dirname(path)

    @staticmethod
    def GetFileName(path):
        return os.path.basename(path)

    @staticmethod
    def GetFileNameWithoutExtension(path):
        return os.path.splitext(os.path.basename(path))[0]

    @staticmethod
    def GetExtension(path):
        return os.path.splitext(path)[1]


class FakeStreamWriter(object):
    """Writes real files, so the generated job / plugin files can be inspected."""

    def __init__(self, filename, append, encoding):
        self.filename = filename
        self.handle = open(filename, "w", encoding="utf-8", newline="\n")

    def WriteLine(self, line):
        self.handle.write(line + "\n")

    def Close(self):
        self.handle.close()


class FakeFrameUtils(object):
    @staticmethod
    def FrameRangeValid(frames):
        return re.match(r"^-?[0-9]+(-(-?[0-9]+)?)?$", str(frames)) is not None


class StringCollection(list):
    def Add(self, value):
        self.append(value)


class FakeDialog(object):
    """Only what the writers read: values by control name."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.messages = []

    def GetValue(self, name):
        return self.values.get(name, "")

    def SetValue(self, name, value):
        self.values[name] = value

    def SetEnabled(self, name, enabled):
        pass

    def SetItems(self, name, items):
        pass

    def ShowMessageBox(self, message, title="", buttons=None):
        self.messages.append((title, message))
        return "Yes"


def LoadSubmissionModule():
    system = types.ModuleType("System")
    system_io = types.ModuleType("System.IO")
    system_io.Path = FakePath
    system_io.StreamWriter = FakeStreamWriter
    system_io.File = types.SimpleNamespace(Exists=os.path.isfile)
    system_io.Directory = types.SimpleNamespace(Exists=os.path.isdir,
                                                CreateDirectory=lambda path: os.makedirs(path, exist_ok=True))

    collections = types.ModuleType("System.Collections.Specialized")
    collections.StringCollection = StringCollection

    text = types.ModuleType("System.Text")
    text.Encoding = types.SimpleNamespace(Unicode="unicode", UTF8="utf8")

    commands = []

    scripting = types.ModuleType("Deadline.Scripting")
    scripting.RepositoryUtils = types.SimpleNamespace(
        # Resolve repository-relative paths onto the working copy, like the real cache does.
        GetRepositoryFilePath=lambda path, custom=False: os.path.join(CUSTOM, str(path).replace("/", os.sep)),
        GetMaximumPriority=lambda: 100,
        GetUsersSettingsDirectory=lambda: tempfile.gettempdir(),
    )
    scripting.FrameUtils = FakeFrameUtils
    scripting.ClientUtils = types.SimpleNamespace(
        GetDeadlineTempPath=lambda: TEMP_PATH,
        GetUsersSettingsDirectory=lambda: tempfile.gettempdir(),
        ExecuteCommandAndGetOutput=lambda args: commands.append(list(args)) or "Submitted",
    )
    scripting.PathUtils = types.SimpleNamespace(IsPathLocal=lambda path: False)

    dialog_module = types.ModuleType("DeadlineUI.Controls.Scripting.DeadlineScriptDialog")
    dialog_module.DeadlineScriptDialog = FakeDialog

    # 'imp' was removed in Python 3.12; the script still imports it because Deadline's own
    # Python is 3.10, so the test provides it.
    imp = types.ModuleType("imp")
    imp.load_source = lambda name, path: types.ModuleType(name)
    integration_ui = types.ModuleType("IntegrationUI")
    integration_ui.IntegrationDialog = lambda: types.SimpleNamespace(
        AddIntegrationTabs=lambda *args, **kwargs: None,
        CloseProjectManagementConnections=lambda *args: None,
        CheckIntegrationSanity=lambda outputFile: True,
        IntegrationProcessingRequested=lambda: False,
        IntegrationGroupBatchRequested=lambda: False,
        WriteIntegrationInfo=lambda writer, index: index,
    )

    for name, module in (
        ("System", system),
        ("System.IO", system_io),
        ("System.Collections.Specialized", collections),
        ("System.Text", text),
        ("Deadline", types.ModuleType("Deadline")),
        ("Deadline.Scripting", scripting),
        ("DeadlineUI", types.ModuleType("DeadlineUI")),
        ("DeadlineUI.Controls", types.ModuleType("DeadlineUI.Controls")),
        ("DeadlineUI.Controls.Scripting", types.ModuleType("DeadlineUI.Controls.Scripting")),
        ("DeadlineUI.Controls.Scripting.DeadlineScriptDialog", dialog_module),
        ("imp", imp),
        ("IntegrationUI", integration_ui),
    ):
        sys.modules[name] = module

    spec = importlib.util.spec_from_file_location("blender_submission", SUBMISSION_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, commands


TEMP_PATH = tempfile.mkdtemp(prefix="dlb_submit_test_")


########################################################################
## Helpers
########################################################################
def Combination(scene="Scene_Main", viewLayer="Beauty", camera="ShotCam_0", **overrides):
    combination = {
        "scene": scene, "view_layer": viewLayer, "camera": camera,
        "engine": "Use Scene Setting", "image_format": "Use Scene Setting",
        "gpu_device": "Use Scene Setting", "resolution_x": 0, "resolution_y": 0,
        "strict_error": False, "marker_override": False,
    }
    combination.update(overrides)
    return combination


def BaseValues(**overrides):
    values = {
        "name": "shot_010",
        "comment": "", "department": "", "pool": "none", "secondary_pool": "", "group": "none",
        "priority": 50, "task_timeout": 0, "auto_timeout": False, "concurrent_tasks": 1,
        "limit_concurrent_tasks": True, "machine_limit": 0, "is_blacklist": False,
        "machine_list": "", "limit_groups": "", "dependencies": "", "on_job_complete": "Nothing",
        "submit_suspended": False, "frames": "1-10", "chunk_size": 1,
        "scene_file": os.path.join(TEMP_PATH, "scene.blend"), "submit_scene": False,
        "output": os.path.join(TEMP_PATH, "render", "beauty_####.png"),
        "threads": 0, "build": "None", "version": "5.2",
        "chain_jobs": False,
        "combinations": [Combination()],
    }
    values.update(overrides)
    return values


def ReadText(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def WriteAndSubmit(module, values):
    module.scriptDialog = FakeDialog()
    jobs, error = module.BuildJobs(values)
    if error != "":
        return None, error, None
    filePairs = module.WriteJobFiles(values, jobs)
    results = module.SubmitJobs(filePairs, values)
    return jobs, "", (filePairs, results)


########################################################################
## Cases
########################################################################
def TestSanitize(module):
    print("")
    print("SanitizeToken / UniqueTokens")
    check(module.SanitizeToken("ShotCam_0") == "ShotCam_0", "a plain name is kept")
    check(module.SanitizeToken("Cam A") == "Cam_A", "a space becomes an underscore")
    check(module.SanitizeToken("a/b:c*d") == "a_b_c_d", "path and wildcard characters are replaced")
    check(module.SanitizeToken("  ..  ") == "..", "dots and dashes survive")
    check(module.SanitizeToken("") == "unnamed", "an empty name does not produce an empty token")

    tokens = module.UniqueTokens(["Cam A", "Cam_A", "Cam-A"])
    check(tokens["Cam A"] == "Cam_A", "the first one keeps the plain token")
    check(tokens["Cam_A"] == "Cam_A_2", "a colliding name is numbered: %s" % tokens["Cam_A"])
    check(tokens["Cam-A"] == "Cam-A", "a name that does not collide is left alone")
    check(len(set(tokens.values())) == 3, "every value gets a distinct token: %s" % tokens)


def TestCombinationsFromPaths(module):
    print("")
    print("CombinationsFromPaths")
    combinations = module.CombinationsFromPaths([("Scene_Main", "Beauty", "ShotCam 0")])
    check(len(combinations) == 1 and combinations[0]["camera"] == "ShotCam 0",
          "a ticked leaf becomes one combination")

    combinations = module.CombinationsFromPaths([
        ("Scene_Main", "Beauty", "ShotCam_0"),
        ("Scene_Main", "Beauty", "ShotCam_0"),
        (" Scene_Main ", " Mask ", " ShotCam_1 "),
    ])
    check(len(combinations) == 2, "duplicates are dropped (%d)" % len(combinations))
    check(combinations[1]["view_layer"] == "Mask", "names are trimmed: %r" % combinations[1]["view_layer"])

    check(module.SelectedCombinations({"combinations": None, "scene": "S", "view_layer": "L", "camera": "C"})
          == [{"scene": "S", "view_layer": "L", "camera": "C"}],
          "without a selection the fallback describes the single job")
    check(len(module.SelectedCombinations({"combinations": [Combination()]})) == 1,
          "with a selection that selection wins")


def TestBuildJobs(module):
    print("")
    print("BuildJobs")

    jobs, error = module.BuildJobs(BaseValues())
    check(error == "" and len(jobs) == 1, "one combination -> one job")
    check(jobs[0]["name"] == "shot_010", "one combination keeps the plain job name")
    check(jobs[0]["output_file"] == os.path.join(TEMP_PATH, "render", "beauty_####.png"),
          "one combination keeps the output path as it is: %s" % jobs[0]["output_file"])

    jobs, error = module.BuildJobs(BaseValues(combinations=[
        Combination(camera="ShotCam_0"), Combination(camera="ShotCam_1")]))
    check(error == "" and len(jobs) == 2, "two combinations -> two jobs")
    check(jobs[0]["name"] == "shot_010 [Beauty / ShotCam_0]",
          "the job name lists the combination: %s" % jobs[0]["name"])
    check(jobs[0]["output_file"] == os.path.join(TEMP_PATH, "render", "beauty_Beauty_ShotCam_0####.png"),
          "the token goes in front of the frame numbers: %s" % jobs[0]["output_file"])
    check(len(set(job["output_file"] for job in jobs)) == 2, "every job gets its own output file")

    jobs, error = module.BuildJobs(BaseValues(combinations=[
        Combination(viewLayer="Beauty", camera="ShotCam_0"),
        Combination(viewLayer="Mask", camera="ShotCam_1", engine="cycles",
                    image_format="PNG", gpu_device="OPTIX", resolution_x=1920, resolution_y=1080,
                    strict_error=True, marker_override=True)]))
    check(len(jobs) == 2 and jobs[1]["name"] == "shot_010 [Mask / ShotCam_1]",
          "the selection does not have to be a full cross product: %s" % jobs[1]["name"])
    check(jobs[1]["engine"] == "cycles" and jobs[1]["image_format"] == "PNG"
          and jobs[1]["gpu_device"] == "OPTIX" and jobs[1]["resolution_x"] == 1920
          and jobs[1]["strict_error"] is True and jobs[1]["marker_override"] is True,
          "each combination carries its own render options")
    check(jobs[0]["engine"] == "Use Scene Setting" and jobs[0]["resolution_x"] == 0,
          "and the other one keeps the .blend values")

    jobs, error = module.BuildJobs(BaseValues(combinations=[
        Combination(scene="Scene_Main"), Combination(scene="Scene_Alt", viewLayer="Layers_A",
                                                     camera="AltCam")]))
    check("Scene_Main" in jobs[0]["name"] and "Scene_Alt" in jobs[1]["name"],
          "a selection across scenes names the scene: %s / %s" % (jobs[0]["name"], jobs[1]["name"]))

    jobs, error = module.BuildJobs(BaseValues(output="", combinations=[
        Combination(camera="ShotCam_0"), Combination(camera="ShotCam_1")]))
    check(error == "" and jobs[0]["output_file"] == "",
          "without an output path the jobs are still built (the caller refuses to submit them)")

    # Per row output file, frame list and frames per task, and the rule that the name suffix
    # only goes into the untouched default path.
    defaultOutput = os.path.join(TEMP_PATH, "render", "beauty_####.png")
    presetOutput = os.path.join(TEMP_PATH, "render", "Scene_Main_Beauty", "Scene_Main_Beauty_ShotCam_1.png")
    jobs, error = module.BuildJobs(BaseValues(combinations=[
        Combination(camera="ShotCam_0", frames="1-20", chunk_size=4),
        Combination(camera="ShotCam_1", output=presetOutput, frames="100-110", chunk_size=2)]))
    check(error == "" and len(jobs) == 2, "two rows with their own output and frames")
    check(jobs[0]["output_file"] == os.path.join(TEMP_PATH, "render", "beauty_Beauty_ShotCam_0####.png"),
          "the row that kept the default path gets the name suffix: %s" % jobs[0]["output_file"])
    check(jobs[1]["output_file"] == os.path.join(TEMP_PATH, "render", "Scene_Main_Beauty",
                                                 "Scene_Main_Beauty_ShotCam_1####.png"),
          "a row with its own path keeps it, with the frame numbers added: %s" % jobs[1]["output_file"])
    check(jobs[0]["frames"] == "1-20" and jobs[0]["chunk_size"] == 4
          and jobs[1]["frames"] == "100-110" and jobs[1]["chunk_size"] == 2,
          "each row keeps its own frame list and frames per task")
    check(jobs[0]["output_file"] != defaultOutput, "and the two rows still write different files")

    jobs, error = module.BuildJobs(BaseValues(combinations=[Combination()]))
    check(jobs[0]["frames"] == "1-10" and jobs[0]["chunk_size"] == 1,
          "a row without its own frames falls back to the dialog's: %s / %s"
          % (jobs[0]["frames"], jobs[0]["chunk_size"]))

    jobs, error = module.BuildJobs(BaseValues(combinations=[
        Combination(camera="Cam_%d" % index) for index in range(120)]))
    check(jobs == [] and str(module.MAX_EXPANDED_JOBS) in error,
          "more jobs than the limit is refused: %s" % error.splitlines()[0])

    jobs, error = module.BuildJobs(BaseValues(combinations=[]))
    check(jobs == [] and error == "", "nothing ticked builds no jobs (the caller reports that)")


def TestControlNames(module):
    print("")
    print("every control the script reads exists")
    with open(SUBMISSION_SCRIPT, "r", encoding="utf-8") as handle:
        source = handle.read()

    created = set(re.findall(r'Add[A-Za-z]*ToGrid\(\s*"([A-Za-z0-9_]+)"', source))
    used = set(re.findall(r'scriptDialog\.(?:GetValue|SetValue|SetItems|SetEnabled|SetVisible)\(\s*"([A-Za-z0-9_]+)"', source))

    check(len(created) >= 25, "found %d controls created by the dialog" % len(created))
    for control in sorted(used):
        check(control in created, "%s is created by the dialog" % control)

    for control in ("ChainJobsBox", "TreeHintBox"):
        check(control in created, "the new control %s exists" % control)

    # The "Will submit" line was dropped: the tree hint and the confirmation cover it.
    for control in ("ExpansionPreviewLabel", "ExpansionPreviewBox"):
        check(control not in created, "the removed control %s is really gone" % control)

    # The copy entry lives in the right-click menu of the table now, not on a button.
    check("CopyOptionsButton" not in created, "the copy button is gone from the dialog")

    # The three combination drop-downs and the seven render option controls are gone: the
    # tree selects the combinations and the table holds the options.
    for control in ("UseTreeBox", "EngineBox", "FormatBox", "GpuBox", "ResolutionXBox",
                    "ResolutionYBox", "StrictErrorBox", "MarkerOverrideBox",
                    "SceneNameBox", "ViewLayerBox", "CameraBox"):
        check(control not in created and control not in used,
              "the removed control %s is really gone" % control)


def TestSingleCombinationOutput(module):
    print("")
    print("one combination: plain name, untouched output, only the selection written")
    commands = COMMANDS
    del commands[:]

    jobs, error, result = WriteAndSubmit(module, BaseValues())
    filePairs, _ = result
    jobInfo, pluginInfo = filePairs[0]

    check(os.path.basename(jobInfo) == "blender_job_info.job",
          "the job file keeps its historical name (%s)" % os.path.basename(jobInfo))
    check(os.path.basename(pluginInfo) == "blender_plugin_info.job",
          "the plugin file keeps its historical name (%s)" % os.path.basename(pluginInfo))

    jobText = ReadText(jobInfo)
    pluginText = ReadText(pluginInfo)

    check("Name=shot_010\n" in jobText, "the job name is written")
    check("OutputFilename0=%s\n" % os.path.join(TEMP_PATH, "render", "beauty_####.png") in jobText,
          "the output file is written")
    check("BatchName" not in jobText, "a single job is not grouped")

    check("RenderScene=Scene_Main\n" in pluginText, "the scene of the combination is written")
    check("ViewLayer=Beauty\n" in pluginText and "Camera=ShotCam_0\n" in pluginText,
          "the ticked view layer and camera are written")
    for key in ("RenderEngine=", "ImageFormat=", "GpuDevice=", "ResolutionX=", "StrictErrorChecking="):
        check(key not in pluginText, "%s is not written while it is not overridden" % key.rstrip("="))

    check(len(commands) == 1 and commands[0] == [jobInfo, pluginInfo],
          "one job is still submitted the historical way: %s"
          % [os.path.basename(argument) for argument in commands[0]])


def TestPerCombinationOptions(module):
    print("")
    print("each combination's options land in its own plugin info file")
    commands = COMMANDS
    del commands[:]

    values = BaseValues(combinations=[
        Combination(camera="ShotCam_0", engine="cycles", resolution_x=1920, resolution_y=1080),
        Combination(camera="ShotCam_1", engine="workbench", image_format="PNG",
                    gpu_device="NONE", strict_error=True),
    ])

    jobs, error, result = WriteAndSubmit(module, values)
    filePairs, _ = result
    check(len(filePairs) == 2, "two job file pairs")

    first = ReadText(filePairs[0][1])
    second = ReadText(filePairs[1][1])

    check("RenderEngine=cycles\n" in first and "ResolutionX=1920\n" in first
          and "ResolutionY=1080\n" in first, "the first job got its own engine and resolution")
    check("RenderEngine=workbench\n" in second and "ImageFormat=PNG\n" in second
          and "GpuDevice=NONE\n" in second and "StrictErrorChecking=True\n" in second,
          "the second job got its own engine, format, GPU and switch")
    check("ImageFormat" not in first, "the first job did not inherit the second one's format")
    check(first.count("StrictErrorChecking") == 0, "the first job did not inherit the switch")


def TestExpandedOutput(module):
    print("")
    print("several combinations: numbered files, BatchName, -SubmitMultipleJobs")
    commands = COMMANDS
    del commands[:]

    values = BaseValues(combinations=[
        Combination(viewLayer="Beauty", camera="ShotCam_0"),
        Combination(viewLayer="Beauty", camera="ShotCam_1"),
        Combination(viewLayer="Mask", camera="ShotCam_0"),
        Combination(viewLayer="Mask", camera="ShotCam_1"),
    ])

    jobs, error, result = WriteAndSubmit(module, values)
    filePairs, results = result
    check(error == "" and len(filePairs) == 4, "four job file pairs")

    names = [os.path.basename(pair[0]) for pair in filePairs]
    check(names[0] == "blender_job_info_001.job" and names[3] == "blender_job_info_004.job",
          "the files are numbered: %s" % names)

    firstJob = ReadText(filePairs[0][0])
    check("Name=shot_010 [Beauty / ShotCam_0]\n" in firstJob, "the job name lists layer and camera")
    check("BatchName=shot_010\n" in firstJob, "the jobs are grouped under the base name")

    check(commands[0][0] == "-SubmitMultipleJobs", "the submission uses -SubmitMultipleJobs")
    check(commands[0].count("-Job") == 4, "one -Job group per combination (%d)" % commands[0].count("-Job"))
    check(results == "Submitted", "the submission output is returned")


def TestChainedSubmission(module):
    print("")
    print("chained submission")
    commands = COMMANDS
    del commands[:]

    values = BaseValues(chain_jobs=True, combinations=[
        Combination(camera="ShotCam_0"), Combination(camera="ShotCam_1")])
    jobs, error, result = WriteAndSubmit(module, values)

    check(error == "", "no error")
    check(commands[0][0] == "-SubmitMultipleJobs" and commands[0][1] == "-Dependent",
          "the chain flag is passed to deadlinecommand: %s" % commands[0][:2])


def TestSceneFileIsAuxiliary(module):
    print("")
    print("'submit the scene with the job' is passed once per job")
    commands = COMMANDS
    del commands[:]

    sceneFile = os.path.join(TEMP_PATH, "scene.blend")
    values = BaseValues(submit_scene=True, combinations=[
        Combination(camera="ShotCam_0"), Combination(camera="ShotCam_1")])
    jobs, error, result = WriteAndSubmit(module, values)
    filePairs, _ = result

    check("SceneFile=" not in ReadText(filePairs[0][1]),
          "the plugin file does not reference the scene when it is submitted with the job")
    check(commands[0].count(sceneFile) == 2,
          "the scene file is attached to every job (%d)" % commands[0].count(sceneFile))


def TestChunkedSubmission(module):
    print("")
    print("a very long command line is split into several submissions")
    commands = COMMANDS
    del commands[:]

    originalLimit = module.MAX_COMMAND_LINE_CHARACTERS
    module.MAX_COMMAND_LINE_CHARACTERS = 6      # three options per job, so two jobs per call
    try:
        values = BaseValues(combinations=[Combination(camera="Cam_%d" % index) for index in range(4)])
        jobs, error, result = WriteAndSubmit(module, values)
        check(error == "" and len(jobs) == 4, "four jobs")
        check(len(commands) == 2, "submitted in two calls (%d)" % len(commands))
        for index, arguments in enumerate(commands):
            check(arguments[0] == "-SubmitMultipleJobs", "call %d is a multi-job submission" % (index + 1))
            check(arguments.count("-Job") == 2, "call %d carries two jobs" % (index + 1))
    finally:
        module.MAX_COMMAND_LINE_CHARACTERS = originalLimit


COMMANDS = []


def main():
    global COMMANDS
    module, commands = LoadSubmissionModule()
    COMMANDS = commands

    print("submission script: %s" % SUBMISSION_SCRIPT)

    TestSanitize(module)
    TestCombinationsFromPaths(module)
    TestBuildJobs(module)
    TestControlNames(module)
    TestSingleCombinationOutput(module)
    TestPerCombinationOptions(module)
    TestExpandedOutput(module)
    TestChainedSubmission(module)
    TestSceneFileIsAuxiliary(module)
    TestChunkedSubmission(module)

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
