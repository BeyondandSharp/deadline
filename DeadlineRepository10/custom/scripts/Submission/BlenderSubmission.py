from __future__ import absolute_import

#
# CUSTOM OVERRIDE of the stock Deadline Blender submission script.
#
# Repository:  <DeadlineRepository>/custom/scripts/Submission/BlenderSubmission.py
# Factory file: <DeadlineRepository>/scripts/Submission/BlenderSubmission.py  (left untouched)
#
# Deadline resolves "scripts/Submission/BlenderSubmission.py" through
# GetRepositoryFilePath(), which checks the repository's "custom" folder first, so the
# Blender add-on picks this dialog up without any change on the artist machines.
#
# On top of the stock dialog this adds the render options that the AWS Deadline Cloud
# Blender submitter exposes (engine, scene, view layer, camera, image format,
# resolution, Cycles GPU device, strict error checking) plus multi-version Blender
# executable selection. Every one of them defaults to "Use Scene Setting", which means
# "render exactly what the .blend file says" - i.e. the stock behaviour.
#
# The Blender-side proxy (custom/submission/Blender/Main/SubmitBlenderToDeadline.py)
# passes a base64 encoded JSON context as a 6th argument describing the scene that is
# being submitted; it is used to populate the drop-downs and to pick sensible defaults.
#

# For Integration UI
import imp
import os
import re
import sys
import time
import typing
import base64
import json

from System import *
from System.IO import Path, StreamWriter, File, Directory
from System.Collections.Specialized import StringCollection
from System.Text import Encoding

from Deadline.Scripting import RepositoryUtils, FrameUtils, ClientUtils, PathUtils

from DeadlineUI.Controls.Scripting.DeadlineScriptDialog import DeadlineScriptDialog

if typing.TYPE_CHECKING:
    from ThinkboxUI.Controls.Scripting.ButtonControl import ButtonControl
imp.load_source( 'IntegrationUI', RepositoryUtils.GetRepositoryFilePath( "submission/Integration/Main/IntegrationUI.py", True ) )
import IntegrationUI

########################################################################
## Globals
########################################################################
scriptDialog = None  # type: DeadlineScriptDialog
settings = None
integration_dialog = None

ProjectManagementOptions = ["Shotgun","FTrack"]
DraftRequested = True

# Sentinel meaning "leave whatever the .blend file has".
USE_SCENE_SETTING = "Use Scene Setting"

ENGINE_ITEMS = ( USE_SCENE_SETTING, "cycles", "eevee", "workbench" )

# Image formats offered by the dialog. Anything not listed can still be set from the
# Monitor (Job Properties -> Blender -> Image Format).
IMAGE_FORMAT_ITEMS = (
    USE_SCENE_SETTING, "BMP", "CINEON", "DDS", "DPX", "HDR", "IRIS", "JPEG", "JPEG2000",
    "OPEN_EXR", "OPEN_EXR_MULTILAYER", "PNG", "TARGA", "TARGA_RAW", "TIFF", "WEBP"
)

# Cycles compute device identifiers, spelled exactly as Blender stores them and as the
# [GpuDevice] control in Blender.options lists them. The Worker upper-cases whatever it
# receives before comparing, so the drop-down here and the one in Job Properties agree.
GPU_DEVICE_ITEMS = ( USE_SCENE_SETTING, "NONE", "CUDA", "OPTIX", "HIP", "ONEAPI", "METAL" )


# Populated from the 6th command line argument, see ParseSubmitContext().
submitContext = {}

# Filled by ReadNamesFromFile() when the dialog was opened without Blender (Monitor or
# Launcher -> Submit -> Blender): the names are then read out of the .blend the user picks,
# with custom/lib/blend_names.py, which does not load the file.
readSceneLayers = {}
readCameras = []
readStatus = ""

########################################################################
## Context helpers
########################################################################
def ParseSubmitContext( args ):
    # type: (list) -> dict
    """Decode the context blob passed by the Blender-side proxy.

    The proxy sends base64 encoded UTF-8 JSON so that spaces and quotes in scene,
    view layer and camera names survive the trip through the command line. Plain
    JSON is accepted as well, which makes the script easy to run by hand.
    """
    if args is None or len( args ) < 6:
        return {}

    raw = args[5]
    if raw is None or str( raw ).strip() == "":
        return {}

    raw = str( raw )

    try:
        raw = base64.b64decode( raw.encode( "utf_8" ) ).decode( "utf_8" )
    except Exception:
        # Not base64 - assume it is already JSON.
        pass

    try:
        parsed = json.loads( raw )
    except Exception:
        return {}

    return parsed if isinstance( parsed, dict ) else {}


def ContextList( key ):
    # type: (str) -> list
    values = submitContext.get( key ) or []

    if not values:
        # No Blender context (Monitor / Launcher submission): use the names that
        # ReadNamesFromFile() read out of the .blend the user selected.
        if key == "scenes":
            values = sorted( readSceneLayers.keys() )
        elif key == "view_layers":
            values = sorted( set( layer for layers in readSceneLayers.values() for layer in layers ) )
        elif key == "cameras":
            values = list( readCameras )

    result = []
    for value in values:
        text = str( value )
        if text != "" and text not in result:
            result.append( text )
    return result


########################################################################
## Turning the ticked combinations into jobs
########################################################################
# A cross product of view layers and cameras can explode into hundreds of jobs, each with
# the whole frame range as tasks, so an accidental Select All is capped.
MAX_EXPANDED_JOBS = 100

# Past this many characters the submission options are handed to deadlinecommand in several
# calls instead of one - long command lines are a documented weak spot.
MAX_COMMAND_LINE_CHARACTERS = 8000


def SanitizeToken( value ):
    # type: (str) -> str
    """Turn a scene / view layer / camera name into a safe part of a file name."""
    token = re.sub( r"[^A-Za-z0-9._-]+", "_", str( value ).strip() )
    token = re.sub( r"_{2,}", "_", token ).strip( "_" )
    return token or "unnamed"


def UniqueTokens( values ):
    # type: (list) -> dict
    """Map every name to a unique token, so two names cannot produce the same file name.

    'Cam A' and 'Cam_A' both sanitize to 'Cam_A'; the second one becomes 'Cam_A_2'.
    """
    tokens = {}
    used = set()

    for value in values:
        base = SanitizeToken( value )
        token = base
        index = 2
        while token.lower() in used:
            token = "%s_%d" % ( base, index )
            index += 1
        used.add( token.lower() )
        tokens[ value ] = token

    return tokens


def SceneNames( sceneName ):
    # type: (str) -> tuple
    """(view layers, cameras) of a scene, from whatever source this submission has.

    Blender is running: the proxy sent a per-scene map, so any scene can be expanded.
    Monitor / Launcher submission: the names were read out of the .blend by
    ReadNamesFromFile(), which gives per-scene view layers and a file-wide camera list
    (cameras can be assigned across scenes in Blender, so a file-wide list is correct).
    """
    entry = ( submitContext.get( "scene_details" ) or {} ).get( sceneName )
    if entry:
        return list( entry.get( "view_layers" ) or [] ), list( entry.get( "cameras" ) or [] )

    activeScene = str( submitContext.get( "active_scene", "" ) )
    if sceneName in ( "", USE_SCENE_SETTING, activeScene ):
        return ContextList( "view_layers" ), ContextList( "cameras" )

    if readSceneLayers and sceneName in readSceneLayers:
        return list( readSceneLayers[ sceneName ] ), list( readCameras )

    return [], []


def SelectedCombinations( values ):
    # type: (dict) -> list
    """The jobs to submit: what the tree has ticked, or the single dialog selection.

    ``values["combinations"]`` is None unless the artist used the combination tree, so the
    plain dialog selection keeps behaving exactly as it always did.
    """
    selected = values.get( "combinations" )
    if selected is not None:
        return list( selected )

    return [ {
        "scene": str( values.get( "scene", "" ) ).strip(),
        "view_layer": str( values.get( "view_layer", "" ) ).strip(),
        "camera": str( values.get( "camera", "" ) ).strip(),
    } ]


def CombinationsFromPaths( paths ):
    # type: (list) -> list
    """Combinations from (scene, view layer, camera) triples, without duplicates.

    Every ticked leaf of the tree is one path. Duplicates would become two jobs writing the
    same output file, so they are dropped here rather than on the farm.
    """
    combinations = []
    seen = set()

    for path in paths:
        scene, viewLayer, camera = ( str( part ).strip() for part in path )
        key = ( scene, viewLayer, camera )
        if key in seen:
            continue
        seen.add( key )
        combinations.append( { "scene": scene, "view_layer": viewLayer, "camera": camera } )

    return combinations


def SplitOutputPath( outputFile ):
    # type: (str) -> tuple
    """(directory, prefix, padding, extension) of an output path template.

    By the time anybody can change it, the dialog has already turned the path Blender sent
    into 'prefix' + '####' + extension (see __main__), so the frame numbers are a run of '#'
    at the end of the name. FrameUtils works on the digits of a concrete frame number and
    would report no padding here, which is why this is done with plain text.
    """
    directory = Path.GetDirectoryName( outputFile )
    name = Path.GetFileNameWithoutExtension( outputFile )
    extension = Path.GetExtension( outputFile )

    match = re.search( r"#+$", name )
    if match:
        return directory, name[ :match.start() ], match.group( 0 ), extension

    if "#" not in outputFile:
        # The dialog adds frame numbers when the path has none of its own.
        return directory, name, "####", extension

    # A '#' somewhere in the middle of the name: leave the name exactly as it is.
    return directory, name, "", extension


def ComposeOutputPath( parts, token ):
    # type: (tuple, str) -> str
    """Insert the job's token in front of the frame numbers.

    Without a token the path is returned exactly as it came in, so a submission that does not
    use the expansion produces the same file name as before. With one, a separator the artist
    already put in front of the frame numbers ('beauty_####') is not doubled up.
    """
    directory, prefix, padding, extension = parts
    if token:
        prefix = prefix.rstrip( "_-." )
    return Path.Combine( directory, prefix + token + padding + extension )


def NormalizeOutputPath( outputFile ):
    # type: (str) -> str
    """Blender's render path with its frame numbers as a run of '#'.

    Blender hands over a concrete frame (``beauty_0001.png``); the rows want the template
    (``beauty_####.png``), which is what the per-job name suffix is inserted into.
    """
    paddingSize = FrameUtils.GetPaddingSizeFromFilename( outputFile )

    padding = ""
    while len( padding ) < paddingSize:
        padding += "#"

    withoutPadding = FrameUtils.GetFilenameWithoutPadding( outputFile )
    directory = Path.GetDirectoryName( withoutPadding )
    prefix = Path.GetFileNameWithoutExtension( withoutPadding )
    extension = Path.GetExtension( withoutPadding )

    return Path.Combine( directory, prefix + padding + extension )


def BuildJobs( values ):
    # type: (dict) -> tuple
    """Turn the current dialog values into one job description per combination.

    Returns (jobs, error). Each job is {name, output_file, scene, view_layer, camera}. The
    preview in the dialog and the actual submission both go through here, so they cannot
    disagree about what is going to be submitted.
    """
    combinations = SelectedCombinations( values )

    if len( combinations ) > MAX_EXPANDED_JOBS:
        return [], ( "This would submit %d jobs (limit %d).\n\nTick fewer view layers or "
                     "cameras in the tree, or submit them in several submissions."
                     % ( len( combinations ), MAX_EXPANDED_JOBS ) )

    # More than one job means the jobs have to be told apart: by their name, and by a token in
    # their output file. A single job keeps the plain name and the untouched output path.
    expanding = len( combinations ) > 1

    # A tree selection can span scenes, and then the scene is part of what identifies a job -
    # two scenes may well have a view layer and camera of the same name.
    multipleScenes = len( set( combination[ "scene" ] for combination in combinations ) ) > 1

    baseName = str( values.get( "name", "" ) ).strip() or "Untitled"
    outputFile = str( values.get( "output", "" ) or "" ).strip()

    # Tokens are computed per dimension value, not per combination: a layer that appears in
    # several combinations must get the same token every time.
    sceneTokens = UniqueTokens( sorted( set( combination[ "scene" ] for combination in combinations ) ) )
    layerTokens = UniqueTokens( sorted( set( combination[ "view_layer" ] for combination in combinations ) ) )
    cameraTokens = UniqueTokens( sorted( set( combination[ "camera" ] for combination in combinations ) ) )

    jobs = []
    for combination in combinations:
        labelParts = []
        tokenParts = []

        if combination[ "scene" ] not in ( "", USE_SCENE_SETTING ) and multipleScenes:
            labelParts.append( combination[ "scene" ] )
            tokenParts.append( sceneTokens[ combination[ "scene" ] ] )

        if combination[ "view_layer" ] not in ( "", USE_SCENE_SETTING ):
            labelParts.append( combination[ "view_layer" ] )
            tokenParts.append( layerTokens[ combination[ "view_layer" ] ] )

        if combination[ "camera" ] not in ( "", USE_SCENE_SETTING ):
            labelParts.append( combination[ "camera" ] )
            tokenParts.append( cameraTokens[ combination[ "camera" ] ] )

        name = baseName
        token = ""
        if expanding:
            if labelParts:
                name = "%s [%s]" % ( baseName, " / ".join( labelParts ) )
            token = ( "_" + "_".join( tokenParts ) ) if tokenParts else ""

        # The row's own output path wins over the dialog default. The name suffix is only
        # added to the untouched default: a path the artist set by hand (or picked from the
        # presets, which already contain the names) is used exactly as it is.
        rowOutput = str( combination.get( "output", "" ) or "" ).strip()
        jobOutput = rowOutput or outputFile
        outputToken = token if jobOutput == outputFile else ""

        job = {
            "name": name,
            "output_file": ComposeOutputPath( SplitOutputPath( jobOutput ), outputToken ) if jobOutput else "",
            "scene": combination[ "scene" ],
            "view_layer": combination[ "view_layer" ],
            "camera": combination[ "camera" ],
            "frames": str( combination.get( "frames", "" ) or values.get( "frames", "" ) ),
            "chunk_size": int( combination.get( "chunk_size", 0 ) or values.get( "chunk_size", 1 ) or 1 ),
        }

        # The render options come from the row as well. The defaults cover a caller that only
        # supplied a combination (the no-Qt fallback and the tests).
        for key in JOB_SETTING_KEYS:
            job[ key ] = combination.get( key, DefaultCombinationSettings( job[ "scene" ] )[ key ] )

        jobs.append( job )

    return jobs, ""


# The width the tree column starts with, so a longer name cannot widen the dialog.
TREE_COLUMN_WIDTH = 280


def ExpansionSummary( jobs, baseName ):
    # type: (list, str) -> str
    """One-line description of what an expansion is about to do, for the dialog."""
    if not jobs:
        return "Nothing to submit."

    if len( jobs ) == 1 and jobs[0][ "name" ] == ( str( baseName ).strip() or "Untitled" ):
        return "Submits one job."

    listed = ", ".join( job[ "name" ] for job in jobs[:2] )
    if len( jobs ) > 2:
        listed += ", ... (+%d more)" % ( len( jobs ) - 2 )

    summary = "Submits %d jobs: %s" % ( len( jobs ), listed )
    if jobs[0][ "output_file" ]:
        summary += "  ->  %s" % Path.GetFileName( jobs[0][ "output_file" ] )
    return summary


########################################################################
## The combination tree: Scene -> View Layer -> Camera
########################################################################
# Filled in by CreateCombinationTree(); stays None when Qt is not importable, in which case
# the dialog falls back to the single selection the three drop-downs describe.
combinationTree = None
combinationTreeError = ""
treeUpdating = False


def QtModules():
    # type: () -> tuple
    """The Qt modules this dialog is built on (Deadline's controls are PyQt5 widgets)."""
    from PyQt5 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def CheckStates():
    # type: () -> tuple
    QtCore = QtModules()[ 0 ]
    return QtCore.Qt.Checked, QtCore.Qt.PartiallyChecked, QtCore.Qt.Unchecked


def SceneList():
    # type: () -> list
    """Every scene the tree offers, each with its own view layers and cameras.

    Blender sends a per-scene map ("scene_details"), so scenes other than the active one can
    be ticked as well: their names come from the file itself, not from what is on screen.
    A Monitor submission gets the same map from custom/lib/blend_names.py once 'Read From
    File' has been pressed.
    """
    details = submitContext.get( "scene_details" ) or {}
    if details:
        return list( details.keys() )

    if readSceneLayers:
        return sorted( readSceneLayers.keys() )

    activeScene = str( submitContext.get( "active_scene", "" ) ).strip()
    return [ activeScene ] if activeScene != "" else []


def MakeCheckable( item, state=None ):
    QtCore = QtModules()[ 0 ]
    item.setFlags( item.flags() | QtCore.Qt.ItemIsUserCheckable )
    item.setCheckState( 0, QtCore.Qt.Unchecked if state is None else state )


def CreateCombinationTree( parentLayout, row ):
    # type: (object, int) -> object
    """Add the tree to the dialog's grid and return it."""
    global combinationTree, combinationTreeError

    try:
        QtCore, QtGui, QtWidgets = QtModules()
    except Exception as error:
        combinationTreeError = "The combination tree needs Qt, which is not available here (%s)." % error
        return None

    tree = QtWidgets.QTreeWidget()
    tree.setColumnCount( 1 )
    tree.setHeaderLabels( [ "Scene / View Layer / Camera" ] )
    tree.setUniformRowHeights( True )
    tree.setMinimumHeight( 180 )
    tree.setContextMenuPolicy( QtCore.Qt.CustomContextMenu )
    tree.customContextMenuRequested.connect( OnCombinationTreeMenu )
    tree.itemChanged.connect( OnCombinationTreeItemChanged )
    # A fixed column, never sized to the content: scene and view layer names of a second scene
    # would otherwise widen the tree - and with it the whole dialog.
    tree.header().setStretchLastSection( False )
    tree.header().setSectionResizeMode( 0, QtWidgets.QHeaderView.Interactive )
    tree.setColumnWidth( 0, TREE_COLUMN_WIDTH )

    parentLayout.addWidget( tree, row, 0, 1, 3 )

    combinationTree = tree
    combinationTreeError = ""
    return tree


def DefaultTickedPaths(scene):
    # type: (str) -> set
    """The combinations ticked when the dialog opens: what Blender currently shows.

    The active view layer with the active camera of the active scene, so the default is
    exactly one job. Other scenes start unticked - they are there to be picked deliberately.
    """
    activeScene = str( submitContext.get( "active_scene", "" ) ).strip()
    if activeScene != "" and scene != activeScene:
        return set()

    layers, cameras = SceneNames( scene )
    if not layers or not cameras:
        return set()

    viewLayer = str( submitContext.get( "active_view_layer", "" ) ).strip()
    if viewLayer not in layers:
        viewLayer = layers[ 0 ]

    camera = str( submitContext.get( "active_camera", "" ) ).strip()
    if camera not in cameras:
        camera = cameras[ 0 ]

    return set( [ ( scene, viewLayer, camera ) ] )


def RefreshCombinationTree():
    # type: () -> None
    """Rebuild the tree from the names this submission knows about, keeping the ticks."""
    global treeUpdating

    if combinationTree is None:
        return

    QtCore, QtGui, QtWidgets = QtModules()
    checked, partial, unchecked = CheckStates()

    previous = set( CheckedPaths() ) if combinationTree.topLevelItemCount() > 0 else set()
    if not previous:
        activeScene = str( submitContext.get( "active_scene", "" ) ).strip()
        scenes = SceneList()
        if activeScene in scenes:
            previous = DefaultTickedPaths( activeScene )
        elif scenes:
            previous = DefaultTickedPaths( scenes[ 0 ] )

    treeUpdating = True
    try:
        combinationTree.clear()

        for scene in SceneList():
            layers, cameras = SceneNames( scene )
            sceneItem = QtWidgets.QTreeWidgetItem( combinationTree, [ scene ] )
            MakeCheckable( sceneItem )

            if not layers or not cameras:
                hint = QtWidgets.QTreeWidgetItem( sceneItem, [ "(no view layers or cameras in this scene)" ] )
                hint.setFlags( QtCore.Qt.NoItemFlags )
                continue

            for layer in layers:
                layerItem = QtWidgets.QTreeWidgetItem( sceneItem, [ layer ] )
                MakeCheckable( layerItem )
                for camera in cameras:
                    cameraItem = QtWidgets.QTreeWidgetItem( layerItem, [ camera ] )
                    MakeCheckable( cameraItem, checked if ( scene, layer, camera ) in previous else unchecked )

        # Every parent follows its children: the default tick is a camera, and the view layer
        # and scene above it have to show as checked / partially checked straight away.
        RefreshTreeStates()

        combinationTree.expandAll()
    finally:
        treeUpdating = False


def CheckedPaths():
    # type: () -> list
    """Every ticked leaf, as (scene, view layer, camera)."""
    checked = CheckStates()[ 0 ]
    paths = []

    if combinationTree is None:
        return paths

    for sceneIndex in range( combinationTree.topLevelItemCount() ):
        sceneItem = combinationTree.topLevelItem( sceneIndex )
        for layerIndex in range( sceneItem.childCount() ):
            layerItem = sceneItem.child( layerIndex )
            for cameraIndex in range( layerItem.childCount() ):
                cameraItem = layerItem.child( cameraIndex )
                if cameraItem.childCount() == 0 and cameraItem.checkState( 0 ) == checked:
                    paths.append( ( sceneItem.text( 0 ), layerItem.text( 0 ), cameraItem.text( 0 ) ) )

    return paths


def SetBranchState( item, state ):
    # type: (object, object) -> None
    item.setCheckState( 0, state )
    for index in range( item.childCount() ):
        SetBranchState( item.child( index ), state )


def InvertBranch( item ):
    # type: (object) -> None
    checked, partial, unchecked = CheckStates()
    if item.childCount() == 0:
        item.setCheckState( 0, unchecked if item.checkState( 0 ) == checked else checked )
        return

    for index in range( item.childCount() ):
        InvertBranch( item.child( index ) )
    RefreshItemState( item )


def RefreshItemState( item ):
    # type: (object) -> None
    """Recompute a parent's checked / partial / unchecked state from its tickable children.

    Non-tickable children (the "(no view layers or cameras)" hint) do not count, so a scene
    that only has that hint does not end up looking half selected.
    """
    QtCore = QtModules()[ 0 ]
    checked, partial, unchecked = CheckStates()

    children = [ item.child( index ) for index in range( item.childCount() )
                 if item.child( index ).flags() & QtCore.Qt.ItemIsUserCheckable ]

    if not children:
        return

    states = [ child.checkState( 0 ) for child in children ]
    if all( state == checked for state in states ):
        item.setCheckState( 0, checked )
    elif all( state == unchecked for state in states ):
        item.setCheckState( 0, unchecked )
    else:
        item.setCheckState( 0, partial )


def RefreshTreeStates():
    # type: () -> None
    """Recompute every parent's state from its children, deepest first.

    Needed after the tree is (re)built from a ticked leaf list and after the right-click menu
    changed a whole branch, so a scene or a view layer never looks unticked while one of the
    combinations below it is going to be rendered.
    """
    if combinationTree is None:
        return

    for sceneIndex in range( combinationTree.topLevelItemCount() ):
        sceneItem = combinationTree.topLevelItem( sceneIndex )
        for layerIndex in range( sceneItem.childCount() ):
            RefreshItemState( sceneItem.child( layerIndex ) )
        RefreshItemState( sceneItem )


def UpdateAncestors( item ):
    # type: (object) -> None
    parent = item.parent()
    while parent is not None:
        RefreshItemState( parent )
        parent = parent.parent()


########################################################################
## One row per ticked combination: the render options of that job
########################################################################
# Filled in by CreateCombinationTable(). The rows are the source of truth for everything a
# job is submitted with apart from its name, so different cameras can use different engines,
# formats, resolutions, frame ranges or output files.
combinationTable = None
combinationTableError = ""
combinationRows = []

# What each combination was last set to, keyed by (scene, view layer, camera). Unticking a
# combination and ticking it again - or rebuilding the tree from a file - brings its settings
# back instead of silently resetting them.
rememberedRowSettings = {}

# What the dialog was opened with, used as the default of each row. Blender sends its render
# output path and frame range; a Monitor submission sends nothing.
defaultOutputFile = ""
defaultFrames = ""
defaultChunkSize = 1

TABLE_HEADERS = ( "Combination", "Output File", "Frame List", "Frames Per Task",
                  "Render Engine", "Image Format", "Cycles GPU",
                  "Res X", "Res Y", "Strict Error", "Override Markers" )

# Starting width of every column. Deliberately not derived from the content: the dialog must not
# change size when a longer name or path shows up (a second scene, for example).
TABLE_COLUMN_WIDTHS = ( 260, 260, 110, 110, 150, 200, 150, 80, 80, 100, 110 )

SETTING_KEYS = ( "output", "frames", "chunk_size",
                 "engine", "image_format", "gpu_device", "resolution_x", "resolution_y",
                 "strict_error", "marker_override" )

# What a job takes from its row. The output file, frame list and frames per task are handled
# separately, because they are partly composed (the name suffix) or counted in characters.
JOB_SETTING_KEYS = ( "engine", "image_format", "gpu_device", "resolution_x", "resolution_y",
                     "strict_error", "marker_override" )

# Which column of the options table holds which setting. Needed to change every selected cell
# of one column when one of them is edited, and to reset a single column.
WIDGET_COLUMNS = { "output": 1, "frames": 2, "chunk_size": 3, "engine": 4, "image_format": 5,
                   "gpu_device": 6, "resolution_x": 7, "resolution_y": 8, "strict_error": 9,
                   "marker_override": 10 }

# Right-click presets for the output file. The placeholders are replaced with the names of
# the row they are applied to, so one preset works for every combination.
# Shown in the menu exactly like this, with {DEFAULT_PATH} standing for the folder of the path
# the dialog was opened with. The '####' is the frame number placeholder the job fills in.
OUTPUT_PRESETS = (
    r"{DEFAULT_PATH}\{Scene}_{ViewLayer}\{Scene}_{ViewLayer}_{Camera}_####",
    r"{DEFAULT_PATH}\{Scene}\{ViewLayer}_{Camera}_####",
    r"{DEFAULT_PATH}\{ViewLayer}_{Camera}_####",
)


def DefaultCombinationSettings( scene="" ):
    # type: (str) -> dict
    """What a new row starts with.

    The resolution is the one the scene has in the file, so the table shows what is going to
    be rendered instead of a zero that means "whatever is in the .blend". Everything else
    starts at 'Use Scene Setting' / the dialog's own values.
    """
    details = ( submitContext.get( "scene_details" ) or {} ).get( scene ) or {}

    resolutionX = int( details.get( "resolution_x", 0 ) or 0 )
    resolutionY = int( details.get( "resolution_y", 0 ) or 0 )

    if not details:
        # A Monitor submission has no per-scene map: the file-wide values describe the active
        # scene, which is the only one whose resolution is known.
        activeScene = str( submitContext.get( "active_scene", "" ) ).strip()
        if scene in ( "", activeScene ):
            resolutionX = int( submitContext.get( "resolution_x", 0 ) or 0 )
            resolutionY = int( submitContext.get( "resolution_y", 0 ) or 0 )

    return {
        "output": defaultOutputFile,
        "frames": defaultFrames,
        "chunk_size": defaultChunkSize,
        "engine": USE_SCENE_SETTING,
        "image_format": USE_SCENE_SETTING,
        "gpu_device": USE_SCENE_SETTING,
        "resolution_x": resolutionX,
        "resolution_y": resolutionY,
        "strict_error": False,
        "marker_override": False,
    }


def CreateCombinationTable( parentLayout, row ):
    # type: (object, int) -> object
    """Add the per-combination options table to the dialog's grid and return it."""
    global combinationTable, combinationTableError

    try:
        QtCore, QtGui, QtWidgets = QtModules()
    except Exception as error:
        combinationTableError = "The per-combination options need Qt, which is not available here (%s)." % error
        return None

    table = QtWidgets.QTableWidget( 0, len( TABLE_HEADERS ) )
    table.setHorizontalHeaderLabels( list( TABLE_HEADERS ) )
    table.verticalHeader().setVisible( False )
    table.setMinimumHeight( 170 )
    table.setAlternatingRowColors( True )
    table.horizontalHeader().setStretchLastSection( True )
    # Fixed starting widths, never sized to the content: a long output path or a view layer name
    # of a second scene would otherwise widen the whole dialog the moment it appears. The user can
    # still drag the section borders.
    for column, width in enumerate( TABLE_COLUMN_WIDTHS ):
        table.setColumnWidth( column, width )
    # Single cells, so one column - or one cell - of a row can be selected and changed on its
    # own, and a multi selection can be edited at once (see OnCellValueChanged).
    table.setSelectionBehavior( QtWidgets.QAbstractItemView.SelectItems )
    table.setSelectionMode( QtWidgets.QAbstractItemView.ExtendedSelection )
    table.setContextMenuPolicy( QtCore.Qt.CustomContextMenu )
    table.customContextMenuRequested.connect( OnCombinationTableMenu )
    # The cells hold real widgets, so the table's own highlight is covered by them; repaint the
    # widgets whenever the selection changes.
    table.itemSelectionChanged.connect( RefreshCellHighlight )

    parentLayout.addWidget( table, row, 0, 1, 3 )

    combinationTable = table
    combinationTableError = ""
    return table


def ReadRowSettings( entry ):
    # type: (dict) -> dict
    """The settings currently shown in one table row."""
    widgets = entry[ "widgets" ]
    return {
        "output": str( widgets[ "output" ].text() ).strip(),
        "frames": str( widgets[ "frames" ].text() ).strip(),
        "chunk_size": int( widgets[ "chunk_size" ].value() ),
        "engine": str( widgets[ "engine" ].currentText() ),
        "image_format": str( widgets[ "image_format" ].currentText() ),
        "gpu_device": str( widgets[ "gpu_device" ].currentText() ),
        "resolution_x": int( widgets[ "resolution_x" ].value() ),
        "resolution_y": int( widgets[ "resolution_y" ].value() ),
        "strict_error": bool( widgets[ "strict_error" ].isChecked() ),
        "marker_override": bool( widgets[ "marker_override" ].isChecked() ),
    }


def ApplyValueToRow( entry, key, value ):
    # type: (dict, str, object) -> None
    """Write one setting into one row's widget, without publishing it to the other cells."""
    global rowUpdating

    widget = entry[ "widgets" ].get( key )
    if widget is None:
        return

    QtCore, QtGui, QtWidgets = QtModules()

    wasUpdating = rowUpdating
    rowUpdating = True
    try:
        if isinstance( widget, QtWidgets.QComboBox ):
            widget.setCurrentText( str( value ) )
        elif isinstance( widget, QtWidgets.QSpinBox ):
            widget.setValue( max( 0, int( value or 0 ) ) )
        elif isinstance( widget, QtWidgets.QCheckBox ):
            widget.setChecked( bool( value ) )
        else:
            widget.setText( str( value ) )
    finally:
        rowUpdating = wasUpdating


def ApplySettingsToRow( entry, settings ):
    # type: (dict, dict) -> None
    """Write a settings dict into an existing row's widgets.

    Only the keys the caller passes are touched, so a one-key dict resets one column and
    nothing else, and a programmatic write never triggers the "change the other selected cells
    too" rule: resetting a column would otherwise spread the first row's default (its own
    scene's resolution, for example) over the rows of another scene.
    """
    for key in SETTING_KEYS:
        if key in settings:
            ApplyValueToRow( entry, key, settings[ key ] )


########################################################################
## Selecting and changing cells
########################################################################
# Set while a value is written into several rows at once, so the resulting signals do not
# start another round of copying.
rowUpdating = False


def KeyForColumn( column ):
    # type: (int) -> str
    """The setting a column holds, or "" for the combination label column."""
    for key, widgetColumn in WIDGET_COLUMNS.items():
        if widgetColumn == column:
            return key

    return ""


def SelectedCells():
    # type: () -> list
    """[(row, key)] of the selected cells that hold a setting."""
    if combinationTable is None:
        return []

    cells = []
    for index in combinationTable.selectedIndexes():
        key = KeyForColumn( index.column() )
        if key != "" and 0 <= index.row() < len( combinationRows ):
            cells.append( ( index.row(), key ) )

    return cells


def SelectedCellRows( key ):
    # type: (str) -> list
    """The rows whose cell of that column is selected."""
    return sorted( set( row for row, cellKey in SelectedCells() if cellKey == key ) )


# The cell a Shift selection extends from. The table's own "current index" is not usable for
# that, because a click on an editor never reaches the table.
selectionAnchor = ( -1, -1 )

# Every widget that belongs to a cell - the editors and their parts - mapped to (row, column,
# key). Rebuilt with the rows, so it never holds a destroyed widget for long.
editorWidgets = {}

# The event filter objects themselves. They have to be kept alive: a filter that Python lets go
# of is deleted, and Qt silently stops calling it.
cellFilters = []

# The blank parts of a cell - the strip next to a drop-down, the padding of a container - that
# only select their cell. A press there is consumed, so it cannot travel on to the table and
# toggle the selection off again (which is what a Ctrl click means to a table).
selectOnlyWidgets = set()

# The palette every text cell had before it was made read-only, so opening it restores the
# normal field look. Rebuilt with the rows.
originalPalettes = {}



# Where the click diagnostics go. Small and overwritten on every submission; it answers
# "why did this field open on the first click" without a debugger.
CELL_LOG_NAME = "BlenderSubmission.log"



def LogCellEvent( message ):
    # type: (str) -> None
    """Append one line to the click log; never let logging break the dialog."""
    try:
        import time

        directory = ClientUtils.GetUsersSettingsDirectory()
        if not Directory.Exists( directory ):
            return

        with open( Path.Combine( directory, CELL_LOG_NAME ), "a" ) as handle:
            handle.write( "%s %s\n" % ( time.strftime( "%H:%M:%S" ), message ) )
    except Exception:
        pass


def ApplyCellPalette( entry, key ):
    # type: (dict, str) -> None
    """Paint a selected cell in the table's highlight colour.

    The cells hold real widgets, so the table's own selection highlight is hidden behind them;
    painting it into the widget is what makes a selected cell visible.
    """
    QtCore, QtGui, QtWidgets = QtModules()

    if combinationTable is None:
        return

    widget = entry[ "widgets" ].get( key )
    if widget is None:
        return

    try:
        row = combinationRows.index( entry )
    except ValueError:
        return

    column = WIDGET_COLUMNS.get( key, -1 )
    if widget not in originalPalettes:
        originalPalettes[ widget ] = QtGui.QPalette( widget.palette() )

    palette = QtGui.QPalette( originalPalettes[ widget ] )

    selected = bool( column >= 0 ) and combinationTable.selectionModel().isSelected(
        combinationTable.model().index( row, column ) )
    if selected:
        tablePalette = combinationTable.palette()
        palette.setColor( QtGui.QPalette.Base, tablePalette.color( QtGui.QPalette.Highlight ) )
        palette.setColor( QtGui.QPalette.Text, tablePalette.color( QtGui.QPalette.HighlightedText ) )
        palette.setColor( QtGui.QPalette.Window, tablePalette.color( QtGui.QPalette.Highlight ) )
        palette.setColor( QtGui.QPalette.Button, tablePalette.color( QtGui.QPalette.Highlight ) )
        palette.setColor( QtGui.QPalette.ButtonText, tablePalette.color( QtGui.QPalette.HighlightedText ) )

    widget.setPalette( palette )


def RefreshCellHighlight():
    # type: () -> None
    """Repaint the selection on every cell; called whenever the selection changes."""
    for entry in combinationRows:
        for key in entry[ "widgets" ]:
            ApplyCellPalette( entry, key )


def SelectCell( row, column, modifiers=None, additive=False ):
    # type: (int, int, object, bool) -> None
    """Select one cell, extend the selection with Ctrl, or select a range with Shift.

    The cells hold real widgets, so the table never sees a click that lands on the editor
    itself; this is called from the editors' event filter as well, which is what makes a cell
    selectable by clicking its drop-down, its check box or its text field.

    With additive=True the cell is only ever added - used for focus changes, which must never
    destroy a selection the artist built with Ctrl.
    """
    global selectionAnchor

    QtCore, QtGui, QtWidgets = QtModules()

    if combinationTable is None or row < 0 or column < 0:
        return

    if modifiers is None:
        modifiers = QtWidgets.QApplication.keyboardModifiers()
    elif callable( modifiers ):
        # QMouseEvent.modifiers is a method: reading it with getattr() hands back the function.
        modifiers = modifiers()

    model = combinationTable.model()
    selection = combinationTable.selectionModel()
    index = model.index( row, column )
    anchorRow, anchorColumn = selectionAnchor

    if additive:
        if not selection.isSelected( index ):
            selection.select( index, QtCore.QItemSelectionModel.Select )

    elif modifiers & QtCore.Qt.ShiftModifier and anchorRow >= 0:
        # A rectangular range from the anchor, like a spreadsheet.
        topLeft = model.index( min( row, anchorRow ), min( column, anchorColumn ) )
        bottomRight = model.index( max( row, anchorRow ), max( column, anchorColumn ) )
        selection.select( QtCore.QItemSelection( topLeft, bottomRight ),
                          QtCore.QItemSelectionModel.Select )

    elif modifiers & QtCore.Qt.ControlModifier:
        selection.select( index, QtCore.QItemSelectionModel.Select )
        selectionAnchor = ( row, column )

    else:
        selection.select( index, QtCore.QItemSelectionModel.ClearAndSelect )
        selectionAnchor = ( row, column )

    # Keep the table's current cell in step, without touching the selection: the view's own
    # setCurrentIndex() uses the click selection command and would clear everything that was
    # picked with Ctrl.
    selection.setCurrentIndex( index, QtCore.QItemSelectionModel.NoUpdate )


def CellForWidget( widget ):
    # type: (object) -> tuple
    """(row, column, key) of the cell a widget belongs to, or (-1, -1, "").

    The widget can be a part of an editor rather than the editor itself: a spin box owns a line
    edit, a drop-down owns its popup view, and the events of those never reach the cell widget.
    """
    cell = editorWidgets.get( widget )
    if cell is not None:
        return cell

    parent = widget.parent() if hasattr( widget, "parent" ) else None
    while parent is not None:
        cell = editorWidgets.get( parent )
        if cell is not None:
            return cell

        parent = parent.parent() if hasattr( parent, "parent" ) else None

    return -1, -1, ""


_editorFilter = None


def HandleCellEvent( watched, event ):
    # type: (object, object) -> bool
    """Turn the events of an editor (or of a part of one) into what the table would have done.

    Returns True when the event was consumed. MouseButtonPress selects the cell and lets the
    editor handle the click itself, FocusIn adds the cell without ever replacing a selection,
    ContextMenu selects it and opens the right-click menu (with the output presets when the cell
    is a path).
    """
    QtCore, QtGui, QtWidgets = QtModules()

    eventType = event.type()
    if eventType not in ( QtCore.QEvent.MouseButtonPress, QtCore.QEvent.FocusIn,
                          QtCore.QEvent.ContextMenu ):
        return False

    if IsInsidePopup( watched ):
        return False

    row, column, key = CellForWidget( watched )
    if row < 0 or combinationTable is None:
        return False

    if eventType == QtCore.QEvent.MouseButtonPress:
        modifiers = getattr( event, "modifiers", None )
        if callable( modifiers ):
            modifiers = modifiers()
        if not modifiers:
            # Some events carry no modifier state; the keyboard still knows.
            modifiers = QtWidgets.QApplication.keyboardModifiers()

        LogCellEvent( "press row=%d column=%d key=%s modifiers=%d" % ( row, column, key, int( modifiers ) ) )

        # Select the cell, then let the editor do what a click normally does. The cells hold
        # real widgets, so the table never sees this click by itself.
        SelectCell( row, column, modifiers )

        # The selection strip is a plain widget: it ignores the press, which would travel on to
        # the table and toggle the cell off again (that is what a Ctrl click means to a table).
        # Swallow it - the strip has no other job than selecting.
        if watched in selectOnlyWidgets:
            return True

        return False

    elif eventType == QtCore.QEvent.FocusIn:
        # Only ever add here: a focus change (which can arrive after the modifier key was
        # released) must not wipe a selection built with Ctrl.
        SelectCell( row, column, None, additive=True )

    else:
        if not combinationTable.selectionModel().isSelected(
                combinationTable.model().index( row, column ) ):
            SelectCell( row, column, QtCore.Qt.NoModifier )
        menu = BuildCombinationTableMenuSafely( row, column, watched if key == "output" else None )
        if menu is not None:
            menu.exec_( watched.mapToGlobal( event.pos() ) )
        return True

    return False


_cellFilterClass = None


def EnsureCellEventFilterClass():
    # type: () -> object
    """The QObject class used as an event filter on the widgets of the options table."""
    global _cellFilterClass

    if _cellFilterClass is not None:
        return _cellFilterClass

    QtCore, QtGui, QtWidgets = QtModules()

    class CellEventFilter( QtCore.QObject ):
        def eventFilter( self, watched, event ):
            try:
                return HandleCellEvent( watched, event )
            except Exception:
                return False

    _cellFilterClass = CellEventFilter
    return _cellFilterClass


def IsInsidePopup( widget ):
    # type: (object) -> bool
    """True for a widget that lives in a popup - the list a drop-down opens.

    A click in there chooses an item; it must not be mistaken for a click on the cell, which
    would replace a Ctrl selection.
    """
    try:
        QtCore, QtGui, QtWidgets = QtModules()
        if not isinstance( widget, QtWidgets.QWidget ):
            return False

        window = widget.window()
        if window is None or not ( window.windowFlags() & QtCore.Qt.Popup ):
            return False

        # The dialog window of Deadline is itself a popup: only a different popup (the list a
        # drop-down opens) counts as "inside a popup".
        if combinationTable is not None and window is combinationTable.window():
            return False

        return True
    except Exception:
        return False


def MakeSelectionStrip( width=18 ):
    # type: (int) -> object
    """A blank strip that is part of a cell: clicking it selects the cell and nothing else.

    A drop-down opens its list wherever it is clicked, so the strip on its left is the place to
    click when the cell only has to be selected - for a multi selection, or before a right-click.
    """
    QtCore, QtGui, QtWidgets = QtModules()

    strip = QtWidgets.QWidget()
    strip.setFixedWidth( width )
    strip.setCursor( QtCore.Qt.PointingHandCursor )
    strip.setToolTip( "Click here to select this cell. The list opens when the value itself is "
                      "clicked." )
    selectOnlyWidgets.add( strip )
    return strip


def WrapWithSelectionStrip( widget, width=18 ):
    # type: (object, int) -> tuple
    """Put a selection strip on the left of an editor; returns (cell widget, strip)."""
    QtCore, QtGui, QtWidgets = QtModules()

    strip = MakeSelectionStrip( width )

    container = QtWidgets.QWidget()
    selectOnlyWidgets.add( container )
    layout = QtWidgets.QHBoxLayout( container )
    layout.setContentsMargins( 0, 0, 0, 0 )
    layout.setSpacing( 0 )
    layout.addWidget( strip )

    if isinstance( widget, QtWidgets.QComboBox ):
        # A drop-down asks for the width of its longest entry by default. In a fixed column that
        # made it reach over the strip of the next cell, so its request is lowered: the text is
        # elided and the strip of the neighbouring column stays visible.
        widget.setSizeAdjustPolicy( QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon )
        widget.setMinimumContentsLength( 0 )

    layout.addWidget( widget, 1 )

    return container, strip


def RegisterCellWidgets( entry, row, cellWidget=None, extraWidgets=() ):
    # type: (dict, int, object, tuple) -> None
    """Remember which cell every editor belongs to, and filter the parts inside them.

    A spin box keeps its text in an internal line edit which Qt creates lazily, and that line
    edit is what receives the click and the focus. Asking for it here (lineEdit()) makes it
    exist now, so the filter can be installed on it; every other child that already exists gets
    one as well, and the lookup walks up from whatever widget received the event.
    """
    QtCore, QtGui, QtWidgets = QtModules()
    filterClass = EnsureCellEventFilterClass()

    targets = []
    for key, widget in entry[ "widgets" ].items():
        targets.append( ( widget, key ) )

        lineEdit = getattr( widget, "lineEdit", None )
        if callable( lineEdit ):
            try:
                inner = lineEdit()
            except Exception:
                inner = None
            if inner is not None:
                targets.append( ( inner, key ) )

        for child in widget.findChildren( QtWidgets.QWidget ):
            if not IsInsidePopup( child ):
                targets.append( ( child, key ) )

    if cellWidget is not None:
        targets.append( ( cellWidget, "output" ) )
        for child in cellWidget.findChildren( QtWidgets.QWidget ):
            if not IsInsidePopup( child ):
                targets.append( ( child, "output" ) )

    for widget, key in extraWidgets:
        targets.append( ( widget, key ) )
        for child in widget.findChildren( QtWidgets.QWidget ):
            if not IsInsidePopup( child ):
                targets.append( ( child, key ) )

    for widget, key in targets:
        editorWidgets[ widget ] = ( row, WIDGET_COLUMNS.get( key, -1 ), key )

        editorFilter = filterClass( widget )
        widget.installEventFilter( editorFilter )
        cellFilters.append( editorFilter )


def ApplyValueToSelectedCells( sourceRow, key, writeValue ):
    # type: (int, str, object, object) -> None
    """Change a cell, then give every other selected cell of that column the same value.

    Only cells of the column that was edited are touched: a selection that spans several
    columns keeps the values of the other columns.
    """
    global rowUpdating

    rows = [ row for row in SelectedCellRows( key ) if row != sourceRow ]
    if not rows:
        return

    rowUpdating = True
    try:
        for row in rows:
            writeValue( combinationRows[ row ][ "widgets" ][ key ] )
    finally:
        rowUpdating = False

    RememberRowSettings()
    RefreshCombinationSummary()


def OnCellValueChanged( entry, key ):
    # type: (dict, str) -> None
    """One cell was edited: copy the value to the other selected cells of the same column."""
    if rowUpdating or combinationTable is None:
        return

    try:
        sourceRow = combinationRows.index( entry )
    except ValueError:
        return

    widget = entry[ "widgets" ][ key ]
    settings = ReadRowSettings( entry )
    value = settings[ key ]

    def WriteValue( target ):
        if isinstance( target, QtModules()[ 2 ].QComboBox ):
            if target.currentText() != str( value ):
                target.setCurrentText( str( value ) )
        elif isinstance( target, QtModules()[ 2 ].QSpinBox ):
            if target.value() != int( value ):
                target.setValue( int( value ) )
        elif isinstance( target, QtModules()[ 2 ].QCheckBox ):
            if target.isChecked() != bool( value ):
                target.setChecked( bool( value ) )
        else:
            if target.text() != str( value ):
                target.setText( str( value ) )

    ApplyValueToSelectedCells( sourceRow, key, WriteValue )


def ConnectCellEditors( entry ):
    # type: (dict) -> None
    """Make every editor of a row publish its value to the other selected cells.

    A drop-down or a check box takes effect as soon as it is changed; a spin box as soon as its
    value changes; a text field when the edit is finished - Enter, or moving to another cell.
    """
    QtCore, QtGui, QtWidgets = QtModules()

    def Connect( key, signal ):
        widget = entry[ "widgets" ][ key ]
        signal.connect( lambda *args, entry=entry, key=key: OnCellValueChanged( entry, key ) )

    for key in ( "engine", "image_format", "gpu_device" ):
        Connect( key, entry[ "widgets" ][ key ].currentIndexChanged )
    for key in ( "resolution_x", "resolution_y", "chunk_size" ):
        Connect( key, entry[ "widgets" ][ key ].valueChanged )
    for key in ( "strict_error", "marker_override" ):
        Connect( key, entry[ "widgets" ][ key ].toggled )
    for key in ( "output", "frames" ):
        Connect( key, entry[ "widgets" ][ key ].editingFinished )


def AddCombinationRow( scene, viewLayer, camera, settings ):
    # type: (str, str, str, dict) -> dict
    QtCore, QtGui, QtWidgets = QtModules()

    row = combinationTable.rowCount()
    combinationTable.insertRow( row )

    # Scene / view layer / camera, so a row can be told apart without reading a tooltip.
    label = QtWidgets.QTableWidgetItem( "%s / %s / %s" % ( scene, viewLayer, camera ) )
    label.setFlags( QtCore.Qt.ItemIsEnabled )
    label.setToolTip( "Scene %s, view layer %s, camera %s - the job renders exactly that."
                      % ( scene, viewLayer, camera ) )
    combinationTable.setItem( row, 0, label )

    output = QtWidgets.QLineEdit( str( settings.get( "output", "" ) ) )
    # The path gives way when the column is narrow: a fixed minimum width would push the browse
    # button out of the cell instead of shrinking the text field.
    output.setMinimumWidth( 0 )
    output.setSizePolicy( QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed )
    output.setToolTip( "Where this job writes its frames. Right-click for presets built from "
                       "the scene, view layer and camera of the row." )
    # The right-click menu of this cell is handled by the editor event filter, together with
    # the other cells, so that cut / copy / paste and the presets stay in one place.

    browse = QtWidgets.QPushButton( "..." )
    browse.setToolTip( "Choose the output file for this combination." )
    browse.setFixedWidth( 28 )
    browse.setSizePolicy( QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed )
    browse.clicked.connect( lambda checked=False, widget=output: BrowseForOutput( widget ) )

    outputCell = QtWidgets.QWidget()
    selectOnlyWidgets.add( outputCell )
    outputLayout = QtWidgets.QHBoxLayout( outputCell )
    outputLayout.setContentsMargins( 0, 0, 0, 0 )
    outputLayout.setSpacing( 2 )
    outputLayout.addWidget( output, 1 )      # everything the button does not need
    outputLayout.addWidget( browse, 0 )      # pinned to the right edge of the cell
    combinationTable.setCellWidget( row, 1, outputCell )

    frames = QtWidgets.QLineEdit( str( settings.get( "frames", "" ) ) )
    frames.setMinimumWidth( 60 )
    frames.setToolTip( "The frames this job renders, e.g. 1-100 or 1-10,20-30." )
    combinationTable.setCellWidget( row, 2, frames )

    chunkSize = QtWidgets.QSpinBox()
    chunkSize.setRange( 1, 1000000 )
    chunkSize.setValue( max( 1, int( settings.get( "chunk_size", 1 ) or 1 ) ) )
    combinationTable.setCellWidget( row, 3, chunkSize )

    engine = QtWidgets.QComboBox()
    engine.addItems( list( ENGINE_ITEMS ) )
    engine.setCurrentText( str( settings.get( "engine", USE_SCENE_SETTING ) ) )
    # A drop-down opens its list wherever it is clicked, so it gets a blank strip on its left
    # that only selects the cell.
    engineCell, engineStrip = WrapWithSelectionStrip( engine )
    combinationTable.setCellWidget( row, 4, engineCell )

    imageFormat = QtWidgets.QComboBox()
    imageFormat.addItems( list( IMAGE_FORMAT_ITEMS ) )
    imageFormat.setCurrentText( str( settings.get( "image_format", USE_SCENE_SETTING ) ) )
    imageFormatCell, imageFormatStrip = WrapWithSelectionStrip( imageFormat )
    combinationTable.setCellWidget( row, 5, imageFormatCell )

    gpuDevice = QtWidgets.QComboBox()
    gpuDevice.addItems( list( GPU_DEVICE_ITEMS ) )
    gpuDevice.setCurrentText( str( settings.get( "gpu_device", USE_SCENE_SETTING ) ) )
    gpuDeviceCell, gpuDeviceStrip = WrapWithSelectionStrip( gpuDevice )
    combinationTable.setCellWidget( row, 6, gpuDeviceCell )

    resolutionX = QtWidgets.QSpinBox()
    resolutionX.setRange( 0, 65536 )
    resolutionX.setValue( max( 0, int( settings.get( "resolution_x", 0 ) or 0 ) ) )
    combinationTable.setCellWidget( row, 7, resolutionX )

    resolutionY = QtWidgets.QSpinBox()
    resolutionY.setRange( 0, 65536 )
    resolutionY.setValue( max( 0, int( settings.get( "resolution_y", 0 ) or 0 ) ) )
    combinationTable.setCellWidget( row, 8, resolutionY )

    strictError = QtWidgets.QCheckBox()
    strictError.setChecked( bool( settings.get( "strict_error", False ) ) )
    combinationTable.setCellWidget( row, 9, strictError )

    markerOverride = QtWidgets.QCheckBox()
    markerOverride.setChecked( bool( settings.get( "marker_override", False ) ) )
    combinationTable.setCellWidget( row, 10, markerOverride )

    entry = {
        "scene": scene,
        "view_layer": viewLayer,
        "camera": camera,
        "widgets": {
            "output": output,
            "frames": frames,
            "chunk_size": chunkSize,
            "engine": engine,
            "image_format": imageFormat,
            "gpu_device": gpuDevice,
            "resolution_x": resolutionX,
            "resolution_y": resolutionY,
            "strict_error": strictError,
            "marker_override": markerOverride,
        },
    }

    ConnectCellEditors( entry )
    RegisterCellWidgets( entry, row, outputCell, (
        ( engineStrip, "engine" ),
        ( imageFormatStrip, "image_format" ),
        ( gpuDeviceStrip, "gpu_device" ),
    ) )
    return entry


def SyncCombinationTable():
    # type: () -> None
    """Make the table's rows match the ticked leaves, keeping the settings already made."""
    global combinationRows

    if combinationTable is None or combinationTableError != "":
        return

    # Nothing changed: leave the rows (and whatever the user is editing) alone.
    if [ ( entry[ "scene" ], entry[ "view_layer" ], entry[ "camera" ] ) for entry in combinationRows ] == CheckedPaths():
        return

    RememberRowSettings()

    combinationTable.setRowCount( 0 )
    combinationRows = []
    editorWidgets.clear()
    originalPalettes.clear()
    selectOnlyWidgets.clear()
    del cellFilters[ : ]

    for scene, viewLayer, camera in CheckedPaths():
        # A combination that was configured before keeps its settings; a new one starts from
        # its own scene's resolution and the output path / frame range the dialog came with.
        settings = rememberedRowSettings.get( ( scene, viewLayer, camera ) ) or DefaultCombinationSettings( scene )
        combinationRows.append( AddCombinationRow( scene, viewLayer, camera, settings ) )


def BrowseForOutput( widget ):
    # type: (object) -> None
    """Choose the output file of one row with a file dialog.

    The frame number placeholder is stripped before the dialog is shown ('####' is not part of
    a real file name) and added again by the job builder when the submission is written.
    """
    QtCore, QtGui, QtWidgets = QtModules()

    suggested = str( widget.text() or "" ).replace( "#", "" )
    chosen, selectedFilter = QtWidgets.QFileDialog.getSaveFileName(
        combinationTable, "Output File", suggested,
        "Images (*.png *.jpg *.jpeg *.exr *.tif *.tiff *.bmp);;All Files (*)" )

    if chosen:
        widget.setText( chosen )
        RememberRowSettings()
        CombinationSelectionChanged()


def RememberRowSettings():
    # type: () -> None
    """Store what every row shows right now, keyed by its combination."""
    for entry in combinationRows:
        rememberedRowSettings[ ( entry[ "scene" ], entry[ "view_layer" ], entry[ "camera" ] ) ] = ReadRowSettings( entry )


def CombinationRows():
    # type: () -> list
    """The ticked combinations together with the settings from their row."""
    rows = []

    for entry in combinationRows:
        row = { "scene": entry[ "scene" ], "view_layer": entry[ "view_layer" ], "camera": entry[ "camera" ] }
        row.update( ReadRowSettings( entry ) )
        rows.append( row )

    if rows or combinationTable is not None:
        return rows

    # Without Qt there is no table; the ticked leaves are still submitted, with the defaults.
    return [ dict( combination, **DefaultCombinationSettings( combination.get( "scene", "" ) ) )
             for combination in CombinationsFromPaths( CheckedPaths() ) ]


def CopyFirstRowToAll():
    # type: () -> None
    """Give every row the first row's settings - except its output file.

    Copying the output path would make every job write the same files. The output column has
    presets for that instead, which use each row's own scene, view layer and camera.
    """
    if not combinationRows:
        return

    settings = ReadRowSettings( combinationRows[ 0 ] )
    for entry in combinationRows[ 1: ]:
        settingsCopy = dict( settings )
        settingsCopy[ "output" ] = ReadRowSettings( entry )[ "output" ]
        ApplySettingsToRow( entry, settingsCopy )

    RememberRowSettings()
    CombinationSelectionChanged()


########################################################################
## Right-click on the options table
########################################################################
def SelectedRowIndexes():
    # type: () -> list
    """The rows the user selected, in order."""
    if combinationTable is None:
        return []

    rows = sorted( set( index.row() for index in combinationTable.selectedIndexes() ) )
    return [ row for row in rows if 0 <= row < len( combinationRows ) ]


def RowIndexForWidget( widget ):
    # type: (object) -> int
    """The row an output cell belongs to, or -1."""
    for index, entry in enumerate( combinationRows ):
        if entry[ "widgets" ].get( "output" ) is widget:
            return index
    return -1


def ResetRowsToDefaults( rows ):
    # type: (list) -> None
    """Put rows back to the defaults of their own scene and of the dialog's output / frames."""
    for row in rows:
        if 0 <= row < len( combinationRows ):
            entry = combinationRows[ row ]
            ApplySettingsToRow( entry, DefaultCombinationSettings( entry[ "scene" ] ) )

    RememberRowSettings()
    CombinationSelectionChanged()


def ResetSelectedCellsToDefaults():
    # type: () -> None
    """Put the selected cells - and only those - back to their default.

    Selecting a whole column, or one cell of several rows, resets exactly that, so a table can
    be brought back to the .blend values without losing the work done in the other columns.
    """
    cells = SelectedCells()
    if not cells:
        return

    for row, key in cells:
        entry = combinationRows[ row ]
        default = DefaultCombinationSettings( entry[ "scene" ] )
        ApplySettingsToRow( entry, { key: default.get( key ) } )

    RememberRowSettings()
    CombinationSelectionChanged()


def ResetSelectedRowsToDefaults():
    # type: () -> None
    """Put every row that has a selected cell back to the defaults."""
    ResetRowsToDefaults( SelectedRowIndexes() )


def OutputBaseAndExtension( path ):
    # type: (str) -> tuple
    """(folder, extension) the output presets are built in.

    A default that ends in an extension is a file path: its directory is the folder and the
    extension is kept. One without an extension is treated as a folder, and the frame number
    placeholders ('#') in it are dropped.
    """
    if Path.GetExtension( path ) == "":
        return path.replace( "#", "" ).rstrip( "/\\" ), ""

    return Path.GetDirectoryName( path ), Path.GetExtension( path )


def OutputPresetsFor( scene, viewLayer, camera ):
    # type: (str, str, str) -> list
    """[(label, path)] output file presets for one combination, built from the default path.

    The label is the pattern as it is written (``{DEFAULT_PATH}\\{Scene}_####.png``) and the
    path is the same pattern with the placeholders of this combination filled in, so the artist
    can see what the preset does before choosing it.
    """
    base = str( defaultOutputFile or "" ).strip()
    if base == "":
        return []

    folder, extension = OutputBaseAndExtension( base )
    names = { "{DEFAULT_PATH}": folder,
              "{Scene}": SanitizeToken( scene ),
              "{ViewLayer}": SanitizeToken( viewLayer ),
              "{Camera}": SanitizeToken( camera ) }

    presets = []
    for pattern in OUTPUT_PRESETS:
        path = pattern + extension
        for token, value in names.items():
            path = path.replace( token, value )

        presets.append( ( pattern + extension, path ) )

    return presets


def ApplyOutputPreset( rows, presetIndex ):
    # type: (list, int) -> None
    """Give every selected row the preset built from its own scene, view layer and camera."""
    for row in rows:
        if not 0 <= row < len( combinationRows ):
            continue
        entry = combinationRows[ row ]
        presets = OutputPresetsFor( entry[ "scene" ], entry[ "view_layer" ], entry[ "camera" ] )
        if presetIndex < len( presets ):
            entry[ "widgets" ][ "output" ].setText( presets[ presetIndex ][ 1 ] )

    RememberRowSettings()
    CombinationSelectionChanged()


def BuildCombinationTableMenu( row=-1, column=-1, widget=None ):
    # type: (int, int, object) -> object
    """The right-click menu of the options table.

    Without a widget (i.e. not on a path cell) it offers the reset entries and the copy entry;
    the output file presets and the text editing entries only belong to the path column.
    """
    QtCore, QtGui, QtWidgets = QtModules()

    menu = QtWidgets.QMenu( widget if widget is not None else combinationTable )

    def Connect( action, handler ):
        action.triggered.connect( lambda checked=False, handler=handler: handler() )

    Connect( menu.addAction( "Reset Selected Cells To Defaults" ), ResetSelectedCellsToDefaults )
    Connect( menu.addAction( "Reset Selected Rows To Defaults" ), ResetSelectedRowsToDefaults )
    Connect( menu.addAction( "Reset All Rows To Defaults" ),
             lambda: ResetRowsToDefaults( list( range( len( combinationRows ) ) ) ) )
    Connect( menu.addAction( "Copy First Row To All" ), CopyFirstRowToAll )

    if widget is not None:
        # This is an output path cell: offer the presets of its own combination.
        entry = combinationRows[ row ] if 0 <= row < len( combinationRows ) else None
        presets = OutputPresetsFor( entry[ "scene" ], entry[ "view_layer" ], entry[ "camera" ] ) if entry else []
        rows = SelectedRowIndexes()
        if row >= 0 and row not in rows:
            rows = [ row ]
        if not rows:
            rows = list( range( len( combinationRows ) ) )

        menu.addSeparator()
        submenu = menu.addMenu( "Output File presets" )
        if presets:
            for index, ( label, path ) in enumerate( presets ):
                Connect( submenu.addAction( label ), lambda index=index: ApplyOutputPreset( rows, index ) )
        else:
            action = submenu.addAction( "No default output path was submitted - type one" )
            action.setEnabled( False )

        # Cut / copy / paste, built here on purpose: taking the actions of
        # widget.createStandardContextMenu() crashes the dialog, because that temporary menu is
        # deleted right away and takes its actions with it.
        menu.addSeparator()
        for label, method in ( ( "Cut", "cut" ), ( "Copy", "copy" ), ( "Paste", "paste" ),
                               ( "Select All", "selectAll" ) ):
            action = menu.addAction( label )
            action.triggered.connect( lambda checked=False, method=method, widget=widget:
                                      getattr( widget, method )() )

    return menu


def BuildCombinationTableMenuSafely( row=-1, column=-1, widget=None ):
    # type: (int, int, object) -> object
    """BuildCombinationTableMenu with the error path covered: a broken menu must not lose the
    dialog (or the submission) to an exception while it is being opened."""
    try:
        return BuildCombinationTableMenu( row, column, widget )
    except Exception as error:
        try:
            QtCore, QtGui, QtWidgets = QtModules()
            menu = QtWidgets.QMenu( widget if widget is not None else combinationTable )
            action = menu.addAction( "The right-click menu could not be built: %s" % error )
            action.setEnabled( False )
            return menu
        except Exception:
            return None


def OnCombinationTableMenu( point ):
    # type: (object) -> None
    if combinationTable is None:
        return

    index = combinationTable.indexAt( point )
    row = index.row() if index.isValid() else -1
    column = index.column() if index.isValid() else -1

    # A right-click that is not on the selection means "this cell": without it the reset
    # entries would act on whatever was selected before.
    if index.isValid() and not combinationTable.selectionModel().isSelected( index ):
        SelectCell( row, column, QtCore.Qt.NoModifier )

    menu = BuildCombinationTableMenuSafely( row, column )
    if menu is not None:
        menu.exec_( combinationTable.viewport().mapToGlobal( point ) )





def OnCombinationTreeItemChanged( item, column ):
    # type: (object, int) -> None
    """Ticking a parent ticks its whole branch; a leaf updates its ancestors."""
    global treeUpdating

    if treeUpdating or combinationTree is None:
        return

    checked, partial, unchecked = CheckStates()
    treeUpdating = True
    try:
        state = item.checkState( 0 )
        if item.childCount() > 0 and state != partial:
            SetBranchState( item, state )
        UpdateAncestors( item )
    finally:
        treeUpdating = False

    CombinationSelectionChanged()


def CombinationTreeMenuEntries( item ):
    # type: (object) -> list
    """(label, callable) for the right-click menu of a tree item.

    Kept separate from showing the menu so that the actions can be tested without a click.
    """
    checked, partial, unchecked = CheckStates()
    root = combinationTree.invisibleRootItem() if combinationTree is not None else None
    target = item if item is not None else root

    return [
        ( "Select All", lambda: SetBranchState( target, checked ) ),
        ( "Select None", lambda: SetBranchState( target, unchecked ) ),
        ( "Invert Selection", lambda: InvertBranch( target ) ),
        ( "Select The Whole Tree", lambda: SetBranchState( root, checked ) ),
        ( "Clear The Whole Tree", lambda: SetBranchState( root, unchecked ) ),
    ]


def OnCombinationTreeMenu( point ):
    # type: (object) -> None
    """The right-click menu: select all / none, invert, only this branch, whole tree."""
    global treeUpdating

    QtCore, QtGui, QtWidgets = QtModules()
    if combinationTree is None:
        return

    item = combinationTree.itemAt( point )
    menu = QtWidgets.QMenu( combinationTree )

    def Run( handler, *args ):
        def Wrapper():
            global treeUpdating
            treeUpdating = True
            try:
                handler()
                RefreshTreeStates()
            finally:
                treeUpdating = False
            CombinationSelectionChanged()
        return Wrapper

    for label, handler in CombinationTreeMenuEntries( item ):
        menu.addAction( label ).triggered.connect( Run( handler ) )

    menu.exec_( combinationTree.viewport().mapToGlobal( point ) )


def TreeHintText():
    # type: () -> str
    """The line under the tree: how much is ticked, or why the tree is incomplete."""
    if combinationTreeError != "":
        return combinationTreeError

    if combinationTree is None:
        return ""

    # The flat "scenes" list has always been sent, the per-scene map has not: when the file
    # clearly has more scenes than the tree shows, the dialog was started by an older
    # Blender-side proxy and the other scenes' names never arrived.
    details = submitContext.get( "scene_details" ) or {}
    allScenes = ContextList( "scenes" )
    if not details and len( allScenes ) > 1:
        return ( "Only %d of the %d scenes could be listed: the Blender-side proxy that opened this "
                 "dialog did not send the other scenes. Update custom/submission/Blender/Main/"
                 "SubmitBlenderToDeadline.py and make sure the repository cache has fetched it "
                 "(deadlinecommand -ExecuteScript custom/tests/report_paths.py shows this)."
                 % ( len( SceneList() ), len( allScenes ) ) )

    scenes = SceneList()
    if not scenes:
        return ( "No scene / view layer / camera names are known yet - press 'Read From File' "
                 "above (or submit from Blender)." )

    total = 0
    for scene in scenes:
        layers, cameras = SceneNames( scene )
        total += len( layers ) * len( cameras )

    return "%d of %d combinations ticked." % ( len( CheckedPaths() ), total )


def LibDirectory():
    """custom/lib - resolved through Deadline first, then relative to this file."""
    readerPath = RepositoryPath( "lib/blend_names.py" )
    if readerPath and os.path.isfile( readerPath ):
        return os.path.dirname( readerPath )

    return os.path.normpath( os.path.join( os.path.dirname( os.path.abspath( __file__ ) ), "..", "..", "lib" ) )


def RepositoryPath( relativePath ):
    # type: (str) -> str
    """Resolve a repository file through Deadline, which also fetches it into the cache.

    The Repository Cache Service only holds files that have been asked for, so importing our
    own files from a __file__-relative path does not work on a cached repository.
    """
    try:
        return str( RepositoryUtils.GetRepositoryFilePath( relativePath, True ) )
    except Exception:
        return ""


def LoadBlendNamesModule():
    # type: () -> object
    """Import custom/lib/blend_names.py, after fetching it and its parser into the cache."""
    libDirectory = LibDirectory()
    if libDirectory not in sys.path:
        sys.path.insert( 0, libDirectory )

    import blend_names

    for relativePath in blend_names.repository_files():
        RepositoryPath( relativePath )

    return blend_names


def FindBlenderExecutable():
    """Blender for the compressed-.blend fallback: environment, then the usual installs."""
    for variable in ( "BAT_BLENDER", "BLENDER_EXECUTABLE" ):
        candidate = os.environ.get( variable, "" )
        if candidate and os.path.isfile( candidate ):
            return candidate

    if os.name == "nt":
        import glob
        found = sorted( glob.glob( r"C:\Program Files\Blender Foundation\Blender*\blender.exe" ) )
        if found:
            return found[-1]

    return ""


########################################################################
## Main Function Called By Deadline
########################################################################
def __main__( *args ):
    # type: (*str) -> None
    global scriptDialog
    global settings
    global submitContext
    global ProjectManagementOptions
    global DraftRequested
    global integration_dialog
    global defaultOutputFile
    global defaultFrames

    submitContext = ParseSubmitContext( args )

    # The output path and the frame range Blender sent are the defaults every table row
    # starts with, so they have to be known before the table is built.
    if len( args ) > 2:
        defaultFrames = str( args[1] )
        defaultOutputFile = NormalizeOutputPath( str( args[2] ) )

    scriptDialog = DeadlineScriptDialog()
    scriptDialog.SetTitle( "Submit Blender Job To Deadline" )
    scriptDialog.SetIcon( scriptDialog.GetIcon( 'Blender' ) )

    scriptDialog.AddTabControl("Tabs", 0, 0)

    scriptDialog.AddTabPage("Job Options")
    scriptDialog.AddGrid()
    scriptDialog.AddControlToGrid( "Separator1", "SeparatorControl", "Job Description", 0, 0, colSpan=2 )

    scriptDialog.AddControlToGrid( "NameLabel", "LabelControl", "Job Name", 1, 0, "The name of your job. This is optional, and if left blank, it will default to 'Untitled'.", False )
    scriptDialog.AddControlToGrid( "NameBox", "TextControl", "Untitled", 1, 1 )

    scriptDialog.AddControlToGrid( "CommentLabel", "LabelControl", "Comment", 2, 0, "A simple description of your job. This is optional and can be left blank.", False )
    scriptDialog.AddControlToGrid( "CommentBox", "TextControl", "", 2, 1 )

    scriptDialog.AddControlToGrid( "DepartmentLabel", "LabelControl", "Department", 3, 0, "The department you belong to. This is optional and can be left blank.", False )
    scriptDialog.AddControlToGrid( "DepartmentBox", "TextControl", "", 3, 1 )
    scriptDialog.EndGrid()

    scriptDialog.AddGrid()
    scriptDialog.AddControlToGrid( "Separator2", "SeparatorControl", "Job Options", 0, 0, colSpan=3 )

    scriptDialog.AddControlToGrid( "PoolLabel", "LabelControl", "Pool", 1, 0, "The pool that your job will be submitted to.", False )
    scriptDialog.AddControlToGrid( "PoolBox", "PoolComboControl", "none", 1, 1 )

    scriptDialog.AddControlToGrid( "SecondaryPoolLabel", "LabelControl", "Secondary Pool", 2, 0, "The secondary pool lets you specify a Pool to use if the primary Pool does not have any available Workers.", False )
    scriptDialog.AddControlToGrid( "SecondaryPoolBox", "SecondaryPoolComboControl", "", 2, 1 )

    scriptDialog.AddControlToGrid( "GroupLabel", "LabelControl", "Group", 3, 0, "The group that your job will be submitted to.", False )
    scriptDialog.AddControlToGrid( "GroupBox", "GroupComboControl", "none", 3, 1 )

    scriptDialog.AddControlToGrid( "PriorityLabel", "LabelControl", "Priority", 4, 0, "A job can have a numeric priority ranging from 0 to 100, where 0 is the lowest priority and 100 is the highest priority.", False )
    scriptDialog.AddRangeControlToGrid( "PriorityBox", "RangeControl", RepositoryUtils.GetMaximumPriority() // 2, 0, RepositoryUtils.GetMaximumPriority(), 0, 1, 4, 1 )

    scriptDialog.AddControlToGrid( "TaskTimeoutLabel", "LabelControl", "Task Timeout", 5, 0, "The number of minutes a Worker has to render a task for this job before it requeues it. Specify 0 for no limit.", False )
    scriptDialog.AddRangeControlToGrid( "TaskTimeoutBox", "RangeControl", 0, 0, 1000000, 0, 1, 5, 1 )
    scriptDialog.AddSelectionControlToGrid( "AutoTimeoutBox", "CheckBoxControl", False, "Enable Auto Task Timeout", 5, 2, "If the Auto Task Timeout is properly configured in the Repository Options, then enabling this will allow a task timeout to be automatically calculated based on the render times of previous frames for the job. " )

    scriptDialog.AddControlToGrid( "ConcurrentTasksLabel", "LabelControl", "Concurrent Tasks", 6, 0, "The number of tasks that can render concurrently on a single Worker. This is useful if the rendering application only uses one thread to render and your Workers have multiple CPUs.", False )
    scriptDialog.AddRangeControlToGrid( "ConcurrentTasksBox", "RangeControl", 1, 1, 16, 0, 1, 6, 1 )
    scriptDialog.AddSelectionControlToGrid( "LimitConcurrentTasksBox", "CheckBoxControl", True, "Limit Tasks To Worker's Task Limit", 6, 2, "If you limit the tasks to a Worker's task limit, then by default, the Worker won't dequeue more tasks then it has CPUs. This task limit can be overridden for individual Workers by an administrator." )

    scriptDialog.AddControlToGrid( "MachineLimitLabel", "LabelControl", "Machine Limit", 7, 0, "Use the Machine Limit to specify the maximum number of machines that can render your job at one time. Specify 0 for no limit.", False )
    scriptDialog.AddRangeControlToGrid( "MachineLimitBox", "RangeControl", 0, 0, 1000000, 0, 1, 7, 1 )
    scriptDialog.AddSelectionControlToGrid( "IsBlacklistBox", "CheckBoxControl", False, "Machine List Is A Deny List", 7, 2, "You can force the job to render on specific machines by using an allow list, or you can avoid specific machines by using a deny list." )

    scriptDialog.AddControlToGrid( "MachineListLabel", "LabelControl", "Machine List", 8, 0, "The list of machines on the deny list or allow list.", False )
    scriptDialog.AddControlToGrid( "MachineListBox", "MachineListControl", "", 8, 1, colSpan=2 )

    scriptDialog.AddControlToGrid( "LimitGroupLabel", "LabelControl", "Limits", 9, 0, "The Limits that your job requires.", False )
    scriptDialog.AddControlToGrid( "LimitGroupBox", "LimitGroupControl", "", 9, 1, colSpan=2 )

    scriptDialog.AddControlToGrid( "DependencyLabel", "LabelControl", "Dependencies", 10, 0, "Specify existing jobs that this job will be dependent on. This job will not start until the specified dependencies finish rendering.", False )
    scriptDialog.AddControlToGrid( "DependencyBox", "DependencyControl", "", 10, 1, colSpan=2 )

    scriptDialog.AddControlToGrid( "OnJobCompleteLabel", "LabelControl", "On Job Complete", 11, 0, "If desired, you can automatically archive or delete the job when it completes.", False )
    scriptDialog.AddControlToGrid( "OnJobCompleteBox", "OnJobCompleteControl", "Nothing", 11, 1 )
    scriptDialog.AddSelectionControlToGrid( "SubmitSuspendedBox", "CheckBoxControl", False, "Submit Job As Suspended", 11, 2, "If enabled, the job will submit in the suspended state. This is useful if you don't want the job to start rendering right away. Just resume it from the Monitor when you want it to render." )
    scriptDialog.EndGrid()

    blenderOptionsGrid = scriptDialog.AddGrid()
    scriptDialog.AddControlToGrid( "Separator3", "SeparatorControl", "Blender Options", 0, 0, colSpan=3 )

    scriptDialog.AddControlToGrid( "SceneLabel", "LabelControl", "Blender File", 1, 0, "The scene file to be rendered.", False )
    scriptDialog.AddSelectionControlToGrid( "SceneBox", "FileBrowserControl", "", "Blender Files (*.blend);;All Files (*)", 1, 1, colSpan=2 )

    scriptDialog.AddSelectionControlToGrid("SubmitSceneBox","CheckBoxControl",False,"Submit Blender Scene File With The Job", 2, 1, colSpan=2, tooltip="If this option is enabled, the scene file will be submitted with the job, and then copied locally to the Worker machine during rendering.")

    scriptDialog.AddControlToGrid( "ThreadsLabel", "LabelControl", "Threads", 3, 0, "The number of threads to use for rendering.", False )
    scriptDialog.AddRangeControlToGrid( "ThreadsBox", "RangeControl", 0, 0, 256, 0, 1, 3, 1, expand=False )

    scriptDialog.AddControlToGrid( "BuildLabel", "LabelControl", "Build To Force", 4, 0, "You can force 32 or 64 bit rendering with this option.", False )
    scriptDialog.AddComboControlToGrid( "BuildBox", "ComboControl", "None", ("None","32bit","64bit"), 4, 1, expand=False )

    ####################################################################
    ## The render tree and the options of each ticked combination
    ####################################################################

    chainJobsBox = scriptDialog.AddSelectionControlToGrid( "ChainJobsBox", "CheckBoxControl", False, "Render One At A Time", 5, 0, colSpan=3, tooltip="Make each submitted job dependent on the previous one, so the combinations are rendered one after the other instead of in parallel. Only used when more than one job is submitted." )
    chainJobsBox.ValueModified.connect( CombinationSelectionChanged )

    scriptDialog.AddControlToGrid( "TreeHintLabel", "LabelControl", "Render combinations", 6, 0,
        "Tick the view layer / camera combinations to render; every ticked item becomes one job. "
        "Ticking a view layer ticks all of its cameras. Right-click an item for Select All, "
        "Select None and Invert Selection.", False )
    scriptDialog.AddControlToGrid( "TreeHintBox", "LabelControl", "", 6, 1, "", False )

    CreateCombinationTree( blenderOptionsGrid, 7 )
    RefreshCombinationTree()

    scriptDialog.AddControlToGrid( "OptionsLabel", "LabelControl", "Options per combination", 8, 0,
        "Every ticked combination gets its own row: output file, frame list, frames per task, engine, "
        "format, GPU, resolution and the two switches. Cells can be selected one by one or with "
        "Ctrl / Shift, and changing one cell of a column changes every selected cell of that "
        "column. Right-click for the reset entries, the copy entry, and - on an output path - the "
        "file name presets.", False )

    CreateCombinationTable( blenderOptionsGrid, 9 )
    SyncCombinationTable()

    if combinationTreeError != "":
        scriptDialog.SetValue( "TreeHintBox", combinationTreeError )
    if combinationTableError != "":
        scriptDialog.SetValue( "TreeHintBox", combinationTableError )

    footerRow = 10

    if not ContextList( "scenes" ):
        # Opened from the Monitor or the Launcher's Submit menu: there is no Blender to ask,
        # so offer to read the names out of the .blend the user picks above.
        scriptDialog.AddControlToGrid( "ReadNamesLabel", "LabelControl", "Scene / layer / camera lists", footerRow, 0,
            "Reads the scene, view layer and camera names out of the selected .blend file. This does not "
            "load the file: only its block index and structure catalog are read.", False )
        readNamesButton = scriptDialog.AddControlToGrid( "ReadNamesButton", "ButtonControl", "Read From File", footerRow, 1, expand=False )
        readNamesButton.ValueModified.connect( ReadNamesFromFile )
        footerRow += 1

        scriptDialog.AddControlToGrid( "NamesStatusLabel", "LabelControl", "", footerRow, 0, "", False )
        scriptDialog.AddControlToGrid( "NamesStatusBox", "LabelControl",
            "Not read yet - press 'Read From File' after choosing the .blend above.", footerRow, 1,
            "The names come from the .blend file. If it cannot be read (for example a "
            "ZStandard-compressed file with no zstd module and no Blender executable), set the "
            "values by hand in Job Properties -> Blender Settings, or use the monitor job script "
            "'Blender Render Options'.", False )
        footerRow += 1

    # The version is a footnote: it only says which Blender submitted this job.
    scriptDialog.AddControlToGrid( "VersionKeyLabel", "LabelControl", "Blender Version", footerRow, 0, "The Blender version that submitted this job. It selects the matching render executable on the Worker.", False )
    scriptDialog.AddControlToGrid( "VersionBox", "LabelControl", str( submitContext.get( "version", "" ) ), footerRow, 1, "The Blender version that submitted this job. It selects the matching render executable on the Worker.", False )

    scriptDialog.EndGrid()
    scriptDialog.EndTabPage()

    integration_dialog = IntegrationUI.IntegrationDialog()
    integration_dialog.AddIntegrationTabs( scriptDialog, "BlenderMonitor", DraftRequested, ProjectManagementOptions, failOnNoTabs=False )
    # Add Project Management and Draft Tabs

    scriptDialog.EndTabControl()

    scriptDialog.AddGrid()
    scriptDialog.AddHorizontalSpacerToGrid( "HSpacer1", 0, 0 )

    submitButton = scriptDialog.AddControlToGrid( "SubmitButton", "ButtonControl", "Submit", 0, 1, expand=False )
    submitButton.ValueModified.connect(SubmitButtonPressed)

    closeButton = scriptDialog.AddControlToGrid( "CloseButton", "ButtonControl", "Close", 0, 2, expand=False )
    # Make sure all the project management connections are closed properly
    closeButton.ValueModified.connect(integration_dialog.CloseProjectManagementConnections)
    closeButton.ValueModified.connect(scriptDialog.closeEvent)

    scriptDialog.EndGrid()

    settings = ("DepartmentBox","CategoryBox","PoolBox","SecondaryPoolBox","GroupBox","PriorityBox","MachineLimitBox","IsBlacklistBox","MachineListBox","LimitGroupBox","SceneBox","ThreadsBox","BuildBox", "SubmitSceneBox", "ChainJobsBox")
    scriptDialog.LoadSettings( GetSettingsFilename(), settings )
    scriptDialog.EnabledStickySaving( settings, GetSettingsFilename() )

    # Fill the combination hint, the options table and the preview.
    CombinationSelectionChanged()

    appSubmission = False
    if len( args ) > 0:
        appSubmission = True

        if args[0] == "":
            scriptDialog.ShowMessageBox( "The Blender scene must be saved before it can be submitted to Deadline.", "Error" )
            return

        scriptDialog.SetValue( "SceneBox", args[0] )
        scriptDialog.SetValue( "NameBox", Path.GetFileNameWithoutExtension( args[0] ) )

        scriptDialog.SetValue( "ThreadsBox", int(args[3]) )

        platform = args[4]
        if platform.find( "64" ) >= 0:
            scriptDialog.SetValue( "BuildBox", "64bit" )
        elif platform.find( "32" ) >= 0 or platform.find( "86" ) >= 0:
            scriptDialog.SetValue( "BuildBox", "32bit" )
        else:
            scriptDialog.SetValue( "BuildBox", "None" )

        # Keep the submission window above all other windows when submitting from another app.
        scriptDialog.MakeTopMost()

    scriptDialog.ShowDialog( appSubmission )

def GetSettingsFilename():
    # type: () -> str
    return Path.Combine( ClientUtils.GetUsersSettingsDirectory(), "BlenderSettings.ini" )

def ReadNamesFromFile():
    # type: () -> None
    """Fill the combination tree from the .blend chosen above.

    Used when the dialog was opened without Blender (Monitor or Launcher -> Submit -> Blender).
    The read goes through custom/lib/blend_names.py: no Blender process is started and no
    scene data is loaded; only the file's block index and its embedded SDNA are parsed.
    """
    global readSceneLayers
    global readCameras
    global readStatus

    sceneFile = str( scriptDialog.GetValue( "SceneBox" ) )
    if sceneFile == "" or not File.Exists( sceneFile ):
        scriptDialog.ShowMessageBox( "Choose a .blend file in the 'Blender File' field above first.",
                                    "Blender Submission" )
        return

    try:
        names = LoadBlendNamesModule().read_names( sceneFile, blender_exe=FindBlenderExecutable() or None )
    except Exception as error:
        readStatus = "Could not read %s" % Path.GetFileName( sceneFile )
        scriptDialog.SetValue( "NamesStatusBox", readStatus )
        scriptDialog.ShowMessageBox(
            "%s\n\nThe scene / view layer / camera fields keep their current values. You can also set "
            "them by hand in Job Properties -> Blender Settings, or afterwards with the monitor job "
            "script 'Blender Render Options'." % error,
            "Blender Submission" )
        return

    readSceneLayers = names["scenes"]
    readCameras = names["cameras"]
    readStatus = "Read %d scene(s) with %s in %.3fs" % ( len( readSceneLayers ), names["source"], names["seconds"] )

    scriptDialog.SetValue( "NamesStatusBox", readStatus )

    # The tree shows the scene this submission is for. Without Blender there is nothing to
    # ask, so the first scene of the file is used.
    if not str( submitContext.get( "active_scene", "" ) ).strip() and readSceneLayers:
        submitContext[ "active_scene" ] = sorted( readSceneLayers.keys() )[ 0 ]

    RefreshCombinationTree()
    CombinationSelectionChanged()

def ValidateRenderOptions():
    # type: () -> str
    """Return an error message when a row's options are inconsistent.

    Everything that can be wrong lives in a row of the options table now: a half specified
    resolution, an invalid frame list, a frame count below one.
    """
    for combination in CombinationRows():
        label = "%s / %s" % ( combination.get( "view_layer", "" ), combination.get( "camera", "" ) )

        resolutionX = int( combination.get( "resolution_x", 0 ) or 0 )
        resolutionY = int( combination.get( "resolution_y", 0 ) or 0 )
        if ( resolutionX > 0 ) != ( resolutionY > 0 ):
            return ( "The combination '%s' has only one of Res X and Res Y set.\n\n"
                     "Set both, or leave both at 0 to use the resolution stored in the .blend file."
                     % label )

        frames = str( combination.get( "frames", "" ) ).strip()
        if frames == "" or not FrameUtils.FrameRangeValid( frames ):
            return ( "The frame list of the combination '%s' (%s) is not valid.\n\n"
                     "Use a range or a list, for example 1-100 or 1-10,20-30." % ( label, frames ) )

        if int( combination.get( "chunk_size", 0 ) or 0 ) < 1:
            return "The frames per task of the combination '%s' has to be at least 1." % label

    return ""

# The lines of the confirmation: exactly the columns of the options table, so what is confirmed
# is what the table showed.
DETAIL_FIELDS = (
    ( "Output File", "output_file" ),
    ( "Frame List", "frames" ),
    ( "Frames Per Task", "chunk_size" ),
    ( "Render Engine", "engine" ),
    ( "Image Format", "image_format" ),
    ( "Cycles GPU", "gpu_device" ),
    ( "Res X", "resolution_x" ),
    ( "Res Y", "resolution_y" ),
    ( "Strict Error", "strict_error" ),
    ( "Override Markers", "marker_override" ),
)


# The engine ids Blender reports, mapped to the names the drop-down shows.
ENGINE_DISPLAY_NAMES = ( ( "BLENDER_EEVEE", "eevee" ), ( "CYCLES", "cycles" ),
                         ( "BLENDER_WORKBENCH", "workbench" ) )


def EngineDisplayName( engine ):
    # type: (str) -> str
    engine = str( engine or "" )
    for prefix, logical in ENGINE_DISPLAY_NAMES:
        if engine.startswith( prefix ):
            return logical

    return engine


def SceneDefaultsFor( scene ):
    # type: (str) -> dict
    """What the .blend file has for that scene: what "Use Scene Setting" stands for.

    Blender sends a per-scene map; a Monitor submission only knows the file-wide values and only
    for the scene that was read, so anything else stays empty and is reported as unknown.
    """
    details = ( submitContext.get( "scene_details" ) or {} ).get( scene ) or {}
    activeScene = str( submitContext.get( "active_scene", "" ) ).strip()
    useFlat = ( not details ) and scene in ( "", activeScene )

    def flat( key, empty ):
        return submitContext.get( key, empty ) if useFlat else empty

    return {
        "engine": EngineDisplayName( details.get( "engine", flat( "engine", "" ) ) ),
        "image_format": str( details.get( "image_format", flat( "image_format", "" ) ) or "" ),
        "gpu_device": str( flat( "gpu_device", "" ) or "" ),
        "resolution_x": int( details.get( "resolution_x", flat( "resolution_x", 0 ) ) or 0 ),
        "resolution_y": int( details.get( "resolution_y", flat( "resolution_y", 0 ) ) or 0 ),
    }


def ResolveDetailValue( key, value, defaults ):
    # type: (str, object, dict) -> str
    """The value a job really renders with.

    A setting left at 'Use Scene Setting' shows the value the .blend file has, and a resolution
    left at 0 shows the scene's own - the confirmation is about what is going to be rendered.
    """
    if isinstance( value, bool ):
        return "yes" if value else "no"

    text = str( value if value is not None else "" )

    if text == USE_SCENE_SETTING:
        resolved = str( defaults.get( key, "" ) or "" )
        return resolved if resolved != "" else "from the .blend file"

    if key in ( "resolution_x", "resolution_y" ) and int( value or 0 ) <= 0:
        resolved = int( defaults.get( key, 0 ) or 0 )
        return str( resolved ) if resolved > 0 else "from the .blend file"

    return text


def SubmissionDetails( jobs, values ):
    # type: (list, dict) -> str
    """The text of the confirmation: every job with all of its options.

    Settings that say "Use Scene Setting" are resolved to what the .blend file has, because the
    confirmation is about what is going to be rendered.
    """
    lines = []

    for index, job in enumerate( jobs, start=1 ):
        lines.append( "%d) %s" % ( index, job.get( "name", "" ) ) )
        lines.append( "   %-17s: %s / %s / %s" % ( "Combination", job.get( "scene", "" ),
                                                    job.get( "view_layer", "" ), job.get( "camera", "" ) ) )

        defaults = SceneDefaultsFor( job.get( "scene", "" ) )
        for label, key in DETAIL_FIELDS:
            lines.append( "   %-17s: %s"
                          % ( label, ResolveDetailValue( key, job.get( key, "" ), defaults ) ) )

        lines.append( "" )

    notes = []
    if values.get( "chain_jobs" ) and len( jobs ) > 1:
        notes.append( "Render One At A Time: every job waits for the one before it." )
    if values.get( "submit_scene" ) and len( jobs ) > 1:
        notes.append( "'Submit Blender Scene File With The Job' is enabled, so each of the %d jobs "
                      "carries its own copy of the .blend file." % len( jobs ) )

    if notes:
        lines.append( "" )
        lines.extend( notes )

    return "\n".join( lines )


def ConfirmationHeadline( jobs ):
    # type: (list) -> str
    return "Submits %d job%s" % ( len( jobs ), "" if len( jobs ) == 1 else "s" )


def BuildConfirmationDialog( jobs, values ):
    # type: (list, dict) -> object
    """The dialog shown before submitting several jobs, or None when Qt is not available."""
    QtCore, QtGui, QtWidgets = QtModules()

    parent = None
    if combinationTable is not None:
        parent = combinationTable.window()

    dialog = QtWidgets.QDialog( parent )
    dialog.setWindowTitle( "Confirm Submission" )

    layout = QtWidgets.QVBoxLayout( dialog )

    headline = QtWidgets.QLabel( ConfirmationHeadline( jobs ) )
    font = QtGui.QFont( headline.font() )
    font.setBold( True )
    headline.setFont( font )
    layout.addWidget( headline )

    # A read-only text box that scrolls both ways: the option lines are long and there can be
    # many jobs, so neither direction may be cut off.
    text = QtWidgets.QPlainTextEdit()
    text.setReadOnly( True )
    text.setLineWrapMode( QtWidgets.QPlainTextEdit.NoWrap )
    text.setHorizontalScrollBarPolicy( QtCore.Qt.ScrollBarAsNeeded )
    text.setVerticalScrollBarPolicy( QtCore.Qt.ScrollBarAsNeeded )
    text.setFont( QtGui.QFontDatabase.systemFont( QtGui.QFontDatabase.FixedFont ) )
    text.setPlainText( SubmissionDetails( jobs, values ) )
    layout.addWidget( text )

    buttons = QtWidgets.QDialogButtonBox( QtWidgets.QDialogButtonBox.Ok |
                                          QtWidgets.QDialogButtonBox.Cancel )
    buttons.accepted.connect( dialog.accept )
    buttons.rejected.connect( dialog.reject )
    layout.addWidget( buttons )

    dialog.resize( 940, 560 )
    return dialog


def ConfirmSubmission( jobs, values ):
    # type: (list, dict) -> bool
    """Ask before submitting; falls back to a message box when Qt is not available."""
    try:
        QtCore, QtGui, QtWidgets = QtModules()
        dialog = BuildConfirmationDialog( jobs, values )
        if dialog is None:
            raise RuntimeError( "no dialog" )
    except Exception as error:
        LogCellEvent( "confirmation dialog not available (%s); using a message box" % error )
        return scriptDialog.ShowMessageBox(
            "%s\n\n%s" % ( ConfirmationHeadline( jobs ), SubmissionDetails( jobs, values ) ),
            "Confirm Submission", ( "Yes", "No" ) ) == "Yes"

    return dialog.exec_() == QtWidgets.QDialog.Accepted


def SubmitButtonPressed(*args):
    # type: (*ButtonControl) -> None
    global scriptDialog
    global integration_dialog

    outputFile = defaultOutputFile
    rows = CombinationRows()
    if rows:
        outputFile = str( rows[ 0 ].get( "output", "" ) or defaultOutputFile )

    # Check if Integration options are valid
    if integration_dialog is not None and not integration_dialog.CheckIntegrationSanity( outputFile ):
        return

    # Check if blender files exist.
    sceneFile = scriptDialog.GetValue( "SceneBox" )
    if( not File.Exists( sceneFile ) ):
        scriptDialog.ShowMessageBox( "The Blender file %s does not exist" % sceneFile, "Error" )
        return
    elif (not scriptDialog.GetValue("SubmitSceneBox") and PathUtils.IsPathLocal(sceneFile)):
        result = scriptDialog.ShowMessageBox( "The Blender file %s is local. Are you sure you want to continue?" % sceneFile, "Warning", ("Yes","No") )
        if(result=="No"):
            return

    # A local output path is worth one warning, not one per row.
    for row in rows:
        rowOutput = str( row.get( "output", "" ) or "" ).strip()
        if rowOutput != "" and PathUtils.IsPathLocal( rowOutput ):
            result = scriptDialog.ShowMessageBox(
                "The output file %s is local. Are you sure you want to continue?" % rowOutput,
                "Warning", ("Yes","No") )
            if( result == "No" ):
                return
            break

    # Check the render options added by the custom submitter (per row).
    renderOptionError = ValidateRenderOptions()
    if( renderOptionError != "" ):
        scriptDialog.ShowMessageBox( renderOptionError, "Error" )
        return

    jobName = scriptDialog.GetValue( "NameBox" )

    # Expand the selection into the jobs that are actually going to be submitted: one per
    # view layer / camera when those boxes are ticked, otherwise exactly one, in which case
    # everything below behaves as it did before this option existed.
    values = CollectValues()
    jobs, expansionError = BuildJobs( values )

    if expansionError != "":
        scriptDialog.ShowMessageBox( expansionError, "Error" )
        return

    if not jobs:
        scriptDialog.ShowMessageBox(
            "Nothing is ticked in the combination tree, so there is nothing to submit.\n\n"
            "Tick at least one scene / view layer / camera combination (right-click an item for "
            "Select All).", "Error" )
        return

    if len( jobs ) > 1 and jobs[0][ "output_file" ] == "":
        scriptDialog.ShowMessageBox(
            "Rendering %d jobs needs an output file: without one every job would write to the path "
            "stored in the .blend file and they would overwrite each other.\n\n"
            "Fill in the 'Output File' column of the table, then submit again." % len( jobs ), "Error" )
        return

    # Two rows writing the same file would silently overwrite each other's frames.
    duplicatePaths = sorted( set(
        job[ "output_file" ] for job in jobs
        if job[ "output_file" ] != "" and
        len( [ other for other in jobs if other[ "output_file" ] == job[ "output_file" ] ] ) > 1 ) )
    if duplicatePaths:
        scriptDialog.ShowMessageBox(
            "These rows write to the same output file:\n\n  %s\n\n"
            "Give them different file names. Right-click an output path for presets that use the "
            "view layer and camera of the row." % "\n  ".join( duplicatePaths ), "Error" )
        return

    if len( jobs ) > 1:
        if not ConfirmSubmission( jobs, values ):
            return

    try:
        filePairs = WriteJobFiles( values, jobs )
    except Exception as error:
        scriptDialog.ShowMessageBox( "Could not write the job files:\n\n%s" % error, "Error" )
        return

    # Now submit the job(s).
    results = SubmitJobs( filePairs, values )
    scriptDialog.ShowMessageBox( results, "Submission Results" )


def CollectValues():
    # type: () -> dict
    """Everything the job files are built from, read once from the dialog.

    Kept separate from the writing so that the expansion preview and the submission itself
    are built from exactly the same values.
    """
    values = {
        "name": str( scriptDialog.GetValue( "NameBox" ) ).strip(),
        "comment": scriptDialog.GetValue( "CommentBox" ),
        "department": scriptDialog.GetValue( "DepartmentBox" ),
        "pool": scriptDialog.GetValue( "PoolBox" ),
        "secondary_pool": scriptDialog.GetValue( "SecondaryPoolBox" ),
        "group": scriptDialog.GetValue( "GroupBox" ),
        "priority": scriptDialog.GetValue( "PriorityBox" ),
        "task_timeout": scriptDialog.GetValue( "TaskTimeoutBox" ),
        "auto_timeout": scriptDialog.GetValue( "AutoTimeoutBox" ),
        "concurrent_tasks": scriptDialog.GetValue( "ConcurrentTasksBox" ),
        "limit_concurrent_tasks": scriptDialog.GetValue( "LimitConcurrentTasksBox" ),
        "machine_limit": scriptDialog.GetValue( "MachineLimitBox" ),
        "is_blacklist": scriptDialog.GetValue( "IsBlacklistBox" ),
        "machine_list": scriptDialog.GetValue( "MachineListBox" ),
        "limit_groups": scriptDialog.GetValue( "LimitGroupBox" ),
        "dependencies": scriptDialog.GetValue( "DependencyBox" ),
        "on_job_complete": scriptDialog.GetValue( "OnJobCompleteBox" ),
        "submit_suspended": scriptDialog.GetValue( "SubmitSuspendedBox" ),
        "frames": defaultFrames,
        "chunk_size": defaultChunkSize,
        "scene_file": scriptDialog.GetValue( "SceneBox" ),
        "submit_scene": scriptDialog.GetValue( "SubmitSceneBox" ),
        "output": defaultOutputFile,
        "threads": scriptDialog.GetValue( "ThreadsBox" ),
        "build": scriptDialog.GetValue( "BuildBox" ),
        "version": scriptDialog.GetValue( "VersionBox" ),
        "chain_jobs": bool( scriptDialog.GetValue( "ChainJobsBox" ) ),
    }

    # Each ticked combination carries its own render options, from its row in the table.
    values[ "combinations" ] = CombinationRows()
    return values

def WriteJobFiles( values, jobs ):
    # type: (dict, list) -> list
    """Write one job info + plugin info file per job; return [(jobFile, pluginFile), ...].

    With a single job the file names and their location are exactly the ones this script has
    always used, so a submission that does not use the expansion is unchanged. With several
    jobs everything goes into one numbered folder belonging to this submission.
    """
    values[ "expanded" ] = len( jobs ) > 1
    values[ "batch_name" ] = str( values[ "name" ] ).strip() or "Untitled"

    tempPath = ClientUtils.GetDeadlineTempPath()

    if len( jobs ) == 1:
        directory = tempPath
        fileNames = [ ( "blender_job_info.job", "blender_plugin_info.job" ) ]
    else:
        directory = Path.Combine( tempPath, "blender_expand_%d" % os.getpid() )
        Directory.CreateDirectory( directory )
        fileNames = [ ( "blender_job_info_%03d.job" % ( index + 1 ),
                        "blender_plugin_info_%03d.job" % ( index + 1 ) )
                      for index in range( len( jobs ) ) ]

    filePairs = []

    for job, ( jobInfoName, pluginInfoName ) in zip( jobs, fileNames ):
        jobInfoFilename = Path.Combine( directory, jobInfoName )
        pluginInfoFilename = Path.Combine( directory, pluginInfoName )

        WriteJobFile( values, job, jobInfoFilename )
        WritePluginInfoFile( values, job, pluginInfoFilename )

        filePairs.append( ( jobInfoFilename, pluginInfoFilename ) )

    return filePairs


def WriteJobFile( values, job, jobInfoFilename ):
    # type: (dict, dict, str) -> None
    """The .job file of one job: the shared options plus this job's name and output path."""
    writer = StreamWriter( jobInfoFilename, False, Encoding.Unicode )
    writer.WriteLine( "Plugin=Blender" )
    writer.WriteLine( "Name=%s" % job[ "name" ] )
    writer.WriteLine( "Comment=%s" % values[ "comment" ] )
    writer.WriteLine( "Department=%s" % values[ "department" ] )
    writer.WriteLine( "Pool=%s" % values[ "pool" ] )
    writer.WriteLine( "SecondaryPool=%s" % values[ "secondary_pool" ] )
    writer.WriteLine( "Group=%s" % values[ "group" ] )
    writer.WriteLine( "Priority=%s" % values[ "priority" ] )
    writer.WriteLine( "TaskTimeoutMinutes=%s" % values[ "task_timeout" ] )
    writer.WriteLine( "EnableAutoTimeout=%s" % values[ "auto_timeout" ] )
    writer.WriteLine( "ConcurrentTasks=%s" % values[ "concurrent_tasks" ] )
    writer.WriteLine( "LimitConcurrentTasksToNumberOfCpus=%s" % values[ "limit_concurrent_tasks" ] )

    writer.WriteLine( "MachineLimit=%s" % values[ "machine_limit" ] )
    if( bool( values[ "is_blacklist" ] ) ):
        writer.WriteLine( "Blacklist=%s" % values[ "machine_list" ] )
    else:
        writer.WriteLine( "Whitelist=%s" % values[ "machine_list" ] )

    writer.WriteLine( "LimitGroups=%s" % values[ "limit_groups" ] )
    writer.WriteLine( "JobDependencies=%s" % values[ "dependencies" ] )
    writer.WriteLine( "OnJobComplete=%s" % values[ "on_job_complete" ] )

    if( bool( values[ "submit_suspended" ] ) ):
        writer.WriteLine( "InitialStatus=Suspended" )

    writer.WriteLine( "Frames=%s" % ( job.get( "frames", "" ) or values[ "frames" ] ) )
    writer.WriteLine( "ChunkSize=%s" % ( int( job.get( "chunk_size", 0 ) or values[ "chunk_size" ] ) ) )

    if job[ "output_file" ] != "":
        writer.WriteLine( "OutputFilename0=%s" % job[ "output_file" ] )

    # Integration
    extraKVPIndex = 0
    groupBatch = False

    if integration_dialog is not None and integration_dialog.IntegrationProcessingRequested():
        extraKVPIndex = integration_dialog.WriteIntegrationInfo( writer, extraKVPIndex )
        groupBatch = groupBatch or integration_dialog.IntegrationGroupBatchRequested()

    # An expansion is always grouped so the Monitor shows the cameras together; a single job
    # is only grouped when the integration asked for it, exactly as before.
    batchName = values[ "batch_name" ] if values[ "expanded" ] else ""
    if groupBatch and batchName == "":
        batchName = job[ "name" ]
    if batchName != "":
        writer.WriteLine( "BatchName=%s\n" % batchName )

    writer.Close()


def WritePluginInfoFile( values, job, pluginInfoFilename ):
    # type: (dict, dict, str) -> None
    """The plugin info file of one job: the shared options plus this job's render selection."""
    writer = StreamWriter( pluginInfoFilename, False, Encoding.Unicode )

    if( not values[ "submit_scene" ] ):
        writer.WriteLine( "SceneFile=" + values[ "scene_file" ] )

    if job[ "output_file" ] != "":
        writer.WriteLine( "OutputFile=%s" % job[ "output_file" ] )

    writer.WriteLine( "Threads=%s" % values[ "threads" ] )
    writer.WriteLine( "Build=%s" % values[ "build" ] )

    # Always written: it selects the [Blender_<version>_RenderExecutable] entry on the
    # Worker, and fills the {version} / {version_full} placeholders of the shared
    # render executable template.
    writer.WriteLine( "Version=%s" % values[ "version" ] )
    versionFull = str( submitContext.get( "version_full", "" ) ).strip()
    if versionFull != "":
        writer.WriteLine( "VersionFull=%s" % versionFull )

    # The remaining options are only written when they differ from "use the .blend
    # value", so jobs that do not override anything look exactly like stock submissions.
    # Every one of them comes from this job's row in the options table.
    for pluginInfoKey, value in (
        ( "RenderEngine", job.get( "engine", USE_SCENE_SETTING ) ),
        ( "RenderScene", job[ "scene" ] ),
        ( "ViewLayer", job[ "view_layer" ] ),
        ( "Camera", job[ "camera" ] ),
        ( "ImageFormat", job.get( "image_format", USE_SCENE_SETTING ) ),
        ( "GpuDevice", job.get( "gpu_device", USE_SCENE_SETTING ) )
    ):
        value = str( value ).strip()
        if value != "" and value != USE_SCENE_SETTING:
            writer.WriteLine( "%s=%s" % ( pluginInfoKey, value ) )

    resolutionX = int( job.get( "resolution_x", 0 ) or 0 )
    resolutionY = int( job.get( "resolution_y", 0 ) or 0 )
    if resolutionX > 0 and resolutionY > 0:
        writer.WriteLine( "ResolutionX=%d" % resolutionX )
        writer.WriteLine( "ResolutionY=%d" % resolutionY )

    if bool( job.get( "strict_error", False ) ):
        writer.WriteLine( "StrictErrorChecking=True" )

    if bool( job.get( "marker_override", False ) ):
        writer.WriteLine( "MarkerOverride=True" )

    # Record the names that existed in the submitted file. Deadline job properties can only
    # build a drop-down from a fixed list of values, and scene / view layer / camera names
    # only exist inside the .blend file, so these are stored as read-only text that the
    # Monitor shows in the Blender Settings page next to the free-text boxes.
    available = {
        "scenes": submitContext.get( "scenes" ) or [],
        "view_layers": submitContext.get( "view_layers" ) or [],
        "cameras": submitContext.get( "cameras" ) or []
    }

    if values[ "expanded" ]:
        # The context lists describe one scene; an expanded job may render a different one,
        # and then the recorded names have to be that scene's names.
        sceneLayers, sceneCameras = SceneNames( job[ "scene" ] )
        if sceneLayers:
            available[ "view_layers" ] = sceneLayers
        if sceneCameras:
            available[ "cameras" ] = sceneCameras

    for contextKey, pluginInfoKey in (
        ( "scenes", "AvailableScenes" ),
        ( "view_layers", "AvailableViewLayers" ),
        ( "cameras", "AvailableCameras" )
    ):
        valuesForKey = available[ contextKey ]
        if valuesForKey:
            writer.WriteLine( "%s=%s" % ( pluginInfoKey, ", ".join( str( value ) for value in valuesForKey ) ) )

    writer.Close()


def SubmitJobs( filePairs, values ):
    # type: (list, dict) -> str
    """Submit the job files and return the output to show.

    One job keeps the command line this script has always used. Several jobs use
    ``-SubmitMultipleJobs`` with one ``-Job <job> <plugin>`` group per job, optionally with
    ``-Dependent`` so every job waits for the previous one.
    """
    auxiliaryFiles = [ values[ "scene_file" ] ] if values[ "submit_scene" ] else []

    if len( filePairs ) == 1:
        arguments = StringCollection()
        arguments.Add( filePairs[0][0] )
        arguments.Add( filePairs[0][1] )
        for auxiliaryFile in auxiliaryFiles:
            arguments.Add( auxiliaryFile )

        return ClientUtils.ExecuteCommandAndGetOutput( arguments )

    options = [ "-SubmitMultipleJobs" ]
    if values[ "chain_jobs" ]:
        options.append( "-Dependent" )
    for jobInfoFilename, pluginInfoFilename in filePairs:
        options.append( "-Job" )
        options.append( jobInfoFilename )
        options.append( pluginInfoFilename )
        for auxiliaryFile in auxiliaryFiles:
            options.append( auxiliaryFile )

    # Very long command lines are split into several calls. Chained submissions are not
    # split, because a chain cannot be continued across two deadlinecommand invocations -
    # the job limit (MAX_EXPANDED_JOBS) keeps that command line well inside the limits.
    if values[ "chain_jobs" ] or sum( len( option ) + 1 for option in options ) <= MAX_COMMAND_LINE_CHARACTERS:
        chunks = [ options ]
    else:
        perJob = ( len( options ) - 1 ) // len( filePairs )
        chunkSize = max( 1, MAX_COMMAND_LINE_CHARACTERS // max( 1, perJob ) )
        chunks = []
        for start in range( 0, len( filePairs ), chunkSize ):
            chunk = list( options[ :1 ] )
            for jobInfoFilename, pluginInfoFilename in filePairs[ start : start + chunkSize ]:
                chunk.append( "-Job" )
                chunk.append( jobInfoFilename )
                chunk.append( pluginInfoFilename )
                for auxiliaryFile in auxiliaryFiles:
                    chunk.append( auxiliaryFile )
            chunks.append( chunk )

    results = []
    for chunk in chunks:
        arguments = StringCollection()
        for option in chunk:
            arguments.Add( option )
        results.append( ClientUtils.ExecuteCommandAndGetOutput( arguments ) )

    return "\n".join( result for result in results if result )


def RefreshCombinationSummary():
    # type: () -> None
    """Update the hint next to the tree without touching the rows or the selection."""
    scriptDialog.SetValue( "TreeHintBox", TreeHintText() )


def CombinationSelectionChanged( *args ):
    # type: (*object) -> None
    """The tree selection changed: bring the options table, the hint and the preview in step."""
    SyncCombinationTable()
    RefreshCombinationSummary()
