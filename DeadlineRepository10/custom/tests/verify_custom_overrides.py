#
# Verification script for the Blender "custom" overrides.
#
# Run it on a machine that is connected to the repository:
#
#   deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\verify_custom_overrides.py"
#
# or from the Monitor: Scripts -> (add it to a script menu) -> Verify Blender Custom Overrides.
#
# It answers the one question that decides whether the whole custom deployment works:
# does Deadline resolve the Blender submission/plugin files under "custom/" or under the
# factory folders? Every check prints PASS or FAIL, and the script ends with a summary.

from __future__ import absolute_import

from System import *
from System.IO import File

from Deadline.Scripting import RepositoryUtils

# relative path -> marker that must be present in the custom version of the file
CUSTOM_FILES = (
    ( "submission/Blender/Main/SubmitBlenderToDeadline.py", "CUSTOM OVERRIDE" ),
    ( "scripts/Submission/BlenderSubmission.py", "CUSTOM OVERRIDE" ),
    ( "plugins/Blender/Blender.py", "CUSTOM OVERRIDE" ),
    ( "plugins/Blender/Blender.param", "CUSTOM OVERRIDE" ),
    ( "plugins/Blender/Blender.options", "CUSTOM OVERRIDE" ),
    ( "plugins/Blender/BlenderRenderScript.py", "deadline-blender-custom" ),
    ( "scripts/Jobs/blender_render_options.py", "Blender Render Options" ),
    ( "lib/blend_names.py", "read_names" ),
    ( "lib/blender_asset_tracer/blendfile/__init__.py", "class BlendFile" ),
    ( "lib/7zip/README.md", "7-Zip-zstd" ),
)

def GetRepositoryFilePathSafe( relativePath, checkCustom ):
    try:
        return str( RepositoryUtils.GetRepositoryFilePath( relativePath, checkCustom ) )
    except Exception as error:
        return "ERROR: %s" % error

def CheckFile( relativePath, marker, failures ):
    customPath = GetRepositoryFilePathSafe( relativePath, True )
    factoryPath = GetRepositoryFilePathSafe( relativePath, False )

    print( "" )
    print( "--- %s" % relativePath )
    print( "    custom  -> %s" % customPath )
    print( "    factory -> %s" % factoryPath )

    if customPath.startswith( "ERROR" ) or not File.Exists( customPath ):
        print( "    FAIL: the custom file does not resolve to an existing file." )
        failures.append( relativePath )
        return

    if "custom" not in customPath.replace( "\\", "/" ).lower():
        print( "    FAIL: Deadline resolved the file outside the repository's custom folder." )
        failures.append( relativePath )
        return

    try:
        content = File.ReadAllText( customPath )
    except Exception as error:
        print( "    FAIL: could not read the file: %s" % error )
        failures.append( relativePath )
        return

    if marker not in content:
        print( "    FAIL: the file was found but it is not the custom version (marker %r missing)." % marker )
        failures.append( relativePath )
        return

    print( "    PASS" )

def __main__( *args ):
    print( "Verifying the Deadline Blender custom overrides..." )

    failures = []
    for relativePath, marker in CUSTOM_FILES:
        CheckFile( relativePath, marker, failures )

    print( "" )
    print( "======================================================================" )
    if failures:
        print( "RESULT: FAILED - %d file(s) did not resolve to the custom folder:" % len( failures ) )
        for relativePath in failures:
            print( "  - %s" % relativePath )
        print( "" )
        print( "Fallbacks, in order of preference:" )
        print( "  1. Job Properties -> Environment -> Custom Plugin Directory (per job)." )
        print( "  2. Copy the custom file over the factory file of the same name." )
        print( "     Keep the master copy in custom/ so a repository upgrade can be re-applied." )
    else:
        print( "RESULT: PASSED - all Blender files resolve to the repository's custom folder." )
    print( "======================================================================" )

    # No return value on purpose: Deadline treats a returned value as an error message.
