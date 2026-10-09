"""Report where Deadline resolves the custom Blender files from, and whether they are readable.

Run this on a machine that shows the problem - the Monitor, a Worker or just a shell:

    deadlinecommand -ExecuteScript "<DeadlineRepository>\\custom\\tests\\report_paths.py"

It prints the repository root, what the Repository Cache Service (RCS) hands out for each of
our files, and the Python that is running the script. Anything that resolves to a cache path
is only updated when the RCS syncs it, which is the usual reason a change "does not take
effect".
"""

import hashlib
import os
import sys

from Deadline.Scripting import RepositoryUtils

FILES = (
    "scripts/Jobs/blender_render_options.py",
    "lib/blend_names.py",
    "lib/blender_asset_tracer/__init__.py",
    "lib/blender_asset_tracer/blendfile/__init__.py",
    "lib/7zip/7z.exe",
    "lib/7zip/7z.dll",
    "plugins/Blender/Blender.py",
    "plugins/Blender/Blender.options",
    "scripts/Submission/BlenderSubmission.py",
)

# (repository file, markers that must be present) - if one is missing, the machine is still
# being served an older copy of that file by the repository cache.
FRESHNESS = (
    ("scripts/Submission/BlenderSubmission.py",
     ("Every scene the tree offers", "combinationTable", "WIDGET_COLUMNS",
      "selectOnlyWidgets", "ApplyCellPalette", "RefreshCellHighlight")),
    ("submission/Blender/Main/SubmitBlenderToDeadline.py",
     ("scene_details", "active_view_layer", "GetSceneDetails")),
    ("scripts/Jobs/blender_render_options.py",
     ("job recorded name lists", "job.JobPlugin")),
)


def __main__():
    print("Python     : %s" % sys.version.replace("\n", " "))
    print("Executable : %s" % sys.executable)

    for call, function in (
        ("GetRepositoryRoot()", lambda: RepositoryUtils.GetRepositoryRoot()),
        ("GetRootDirectory('scripts/General')", lambda: RepositoryUtils.GetRootDirectory("scripts/General")),
    ):
        try:
            print("%-36s : %s" % (call, function()))
        except Exception as error:
            print("%-36s : <error> %s" % (call, error))

    print("")
    print("%-52s %-8s %s" % ("file", "exists", "resolved path"))
    for relative in FILES:
        for checkCustom in (True, False):
            try:
                path = str(RepositoryUtils.GetRepositoryFilePath(relative, checkCustom))
            except Exception as error:
                path = "<error> %s" % str(error).splitlines()[0]
            print("%-52s %-8s %s" % ("%s [custom=%s]" % (relative, checkCustom),
                                     os.path.isfile(path), path))

    print("")
    print("freshness of the files that decide the submission dialog:")
    print("(%s is the copy the Monitor and Blender actually run - compare the hash with the file"
          % "the resolved path")
    print(" you deployed to the repository; 'STALE' means that copy does not contain the marker)")
    for relative, markers in FRESHNESS:
        path = str(RepositoryUtils.GetRepositoryFilePath(relative, True))
        if not os.path.isfile(path):
            print("  %s: <not found>" % relative)
            continue

        with open(path, "r", errors="replace") as handle:
            text = handle.read()

        missing = [marker for marker in markers if marker not in text]
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

        print("  %s" % relative)
        print("      %s" % ("up to date" if not missing else "STALE: missing %s" % missing))
        print("      %d bytes, sha256 %s" % (len(text), digest[:16]))
        print("      %s" % path)

    print("")
    print("to compare with the repository itself, hash the deployed file the same way:")
    print("  powershell -c \"(Get-FileHash '<repository>\\scripts\\Submission\\BlenderSubmission.py'")
    print("                 -Algorithm SHA256).Hash\"   # the first 16 characters must match")
    return 0
