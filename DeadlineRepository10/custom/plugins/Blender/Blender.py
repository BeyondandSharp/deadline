#!/usr/bin/env python3
#
# CUSTOM OVERRIDE of the stock Deadline Blender application plugin.
#
# Repository:  <DeadlineRepository>/custom/plugins/Blender/Blender.py
# Factory file: <DeadlineRepository>/plugins/Blender/Blender.py  (left untouched)
#
# Deadline loads scripts/plugins from the repository's "custom" folder in preference
# to the factory folders when they share the same name, and repository upgrades do
# not touch "custom". See custom/README.md for the full story.
#
# What this override adds on top of the stock plugin:
#   1. Per-version render executables (Blender_<major.minor>_RenderExecutable),
#      using the same pattern the shipped Nuke plugin uses for its versions.
#   2. Job-level render options (engine / scene / view layer / camera / format /
#      resolution / GPU device / marker override) are handed to Blender through
#      environment variables consumed by BlenderRenderScript.py, which runs inside
#      Blender before the command-line render starts.
#   3. "--python-exit-code 1" so an exception inside that script fails the task
#      instead of exiting 0 and looking like a successful render.
#   4. Optional strict error checking (off by default) and EEVEE sample progress.
#   5. Fixes the stock "SupressOutput" typo so the [SuppressOutput] plugin
#      configuration entry actually has an effect.
#
# Every new option defaults to "do nothing", so jobs that do not use them render
# exactly as they did with the stock plugin.

from __future__ import absolute_import
from System import *
from System.Diagnostics import *
from System.IO import *

from Deadline.Plugins import DeadlinePlugin, PluginType
from Deadline.Scripting import RepositoryUtils, SystemUtils, FileUtils, StringUtils

import os
import re
import sys

# Location of the render setup script, relative to the repository root.
# It is looked up in "custom" first, then in the factory folders.
RENDER_SCRIPT_RELATIVE_PATH = "plugins/Blender/BlenderRenderScript.py"

# Placeholders substituted into the render executable paths of the
# [Blender_RenderExecutable] template. They are deliberately NOT the Deadline path
# mapping token syntax (${type:name}), which only applies to path mapping replacement
# paths and has no token for the submitting application version.
#   {version}      = major.minor of the submitting Blender, e.g. "4.2"
#   {version_full} = full version, e.g. "4.2.1"
VERSION_PLACEHOLDER = "{version}"
VERSION_FULL_PLACEHOLDER = "{version_full}"

# Plugin info key -> (option name on the Blender command line, environment variable).
#
# The options are passed BOTH ways on purpose:
#   * as "name=value" tokens after "--" on the Blender command line, which is what the
#     script actually relies on;
#   * as DLB_* environment variables, as a second channel.
# Environment variables alone turned out to be unreliable here (a task ran with every
# DLB_* variable missing even though the plugin set them), so the command line is the
# primary transport and the variables are the fallback.
#
# Values equal to one of UNSET_VALUES are not forwarded, so the script keeps the value
# stored in the .blend file.
RENDER_OPTION_KEYS = (
    ("RenderEngine", "engine", "DLB_RENDER_ENGINE"),
    ("RenderScene", "scene", "DLB_RENDER_SCENE"),
    ("ViewLayer", "layer", "DLB_VIEW_LAYER"),
    ("Camera", "camera", "DLB_CAMERA"),
    ("ImageFormat", "format", "DLB_IMAGE_FORMAT"),
    ("ResolutionX", "resx", "DLB_RESOLUTION_X"),
    ("ResolutionY", "resy", "DLB_RESOLUTION_Y"),
    ("GpuDevice", "gpu", "DLB_GPU_DEVICE"),
)

UNSET_VALUES = ("", "Use Scene Setting", "Use Scene Settings")

# Matches the adaptor regex used by AWS Deadline Cloud for the same purpose.
EEVEE_PROGRESS_REGEX = "^Fra:.*Rendering\\s([0-9]+)\\s/\\s([0-9]+)\\ssamples$"

DEFAULT_STRICT_ERROR_REGEX = ".*Error:.*"

def GetDeadlinePlugin():
    return BlenderPlugin()

def CleanupDeadlinePlugin( deadlinePlugin ):
    deadlinePlugin.Cleanup()

class BlenderPlugin(DeadlinePlugin):
    frameCount = 0
    finishedFrameCount = 0

    def __init__(self):
        if sys.version_info.major == 3:
            super().__init__()
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderExecutableCallback += self.RenderExecutable
        self.RenderArgumentCallback += self.RenderArgument
        self.PreRenderTasksCallback += self.PreRenderTasks
        self.PostRenderTasksCallback += self.PostRenderTasks

    def Cleanup(self):
        for stdoutHandler in self.StdoutHandlers:
            del stdoutHandler.HandleCallback

        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback
        del self.PreRenderTasksCallback
        del self.PostRenderTasksCallback

    def InitializeProcess(self):
        self.SingleFramesOnly = False
        self.StdoutHandling = True

        #Std out handlers
        self.AddStdoutHandlerCallback(".*Tile ([0-9]+)/([0-9]+).*").HandleCallback += self.HandleTileProgress
        self.AddStdoutHandlerCallback(".*Sample ([0-9]+)/([0-9]+).*").HandleCallback += self.HandleSampleProgress
        self.AddStdoutHandlerCallback(EEVEE_PROGRESS_REGEX).HandleCallback += self.HandleSampleProgress
        self.AddStdoutHandlerCallback(".*Scene, Part ([0-9]+)-([0-9]+).*").HandleCallback += self.HandleSceneProgress
        self.AddStdoutHandlerCallback(".*Saved:.*").HandleCallback += self.HandleStdoutSaved
        self.AddStdoutHandlerCallback("Unable to open.*").HandleCallback += self.HandleStdoutFailed
        self.AddStdoutHandlerCallback("Failed to read blend file.*").HandleCallback += self.HandleStdoutFailed
        self.AddStdoutHandlerCallback(".*Unable to create directory.*").HandleCallback += self.HandleStdoutFailed

        # Strict error checking. The handler is always registered (the pattern comes
        # from the plugin configuration) but only fails the render when the job
        # opted in, mirroring how the shipped Softimage plugin does it.
        strictRegex = self.GetConfigEntryWithDefault( "Blender_StrictErrorRegex", DEFAULT_STRICT_ERROR_REGEX ).strip()
        if strictRegex != "":
            self.AddStdoutHandlerCallback( strictRegex ).HandleCallback += self.HandleStdoutError

    ########################################################################
    ## Render executable (multi-version aware, falls back to stock behaviour)
    ########################################################################

    def ExpandVersionPlaceholders( self, executableList, version, versionFull ):
        """Substitute {version} / {version_full} in a render executable list.

        The list may be separated by semicolons or by newlines, because
        multilinemultifilename configuration entries are edited as one path per line
        in the Monitor while the shipped defaults use semicolons.

        Entries whose placeholder cannot be filled (no version on the job, e.g. a job
        submitted from the Monitor) are dropped instead of being searched for
        literally, and reported once so the reason is visible in the task log.
        """
        entries = re.split( r"[;\r\n]+", executableList or "" )
        expanded = []
        skipped = []

        for entry in entries:
            entry = entry.strip()
            if entry == "":
                continue

            needsVersion = VERSION_PLACEHOLDER in entry
            needsVersionFull = VERSION_FULL_PLACEHOLDER in entry

            if ( needsVersion and version == "" ) or ( needsVersionFull and versionFull == "" ):
                skipped.append( entry )
                continue

            # Replace the longer placeholder first so it can never be clipped by the
            # shorter one.
            entry = entry.replace( VERSION_FULL_PLACEHOLDER, versionFull )
            entry = entry.replace( VERSION_PLACEHOLDER, version )
            expanded.append( entry )

        if skipped:
            self.LogInfo( "Skipped %d render executable path(s) whose version placeholder could not be filled: %s" % ( len( skipped ), "; ".join( skipped ) ) )

        return ";".join( expanded )

    def RenderExecutable(self):
        build = self.GetPluginInfoEntryWithDefault( "Build", "None" ).lower()
        version = self.GetPluginInfoEntryWithDefault( "Version", "" ).strip().lower()
        versionFull = self.GetPluginInfoEntryWithDefault( "VersionFull", "" ).strip()

        # A dedicated [Blender_<version>_RenderExecutable] section wins when an
        # administrator added one (for an unusually named installation); otherwise the
        # shared template is used with the submitting version substituted into it.
        executableList = ""
        if version != "":
            executableList = ( self.GetConfigEntryWithDefault( "Blender_%s_RenderExecutable" % version, "" ) or "" ).strip()
            if executableList != "":
                self.LogInfo( "Using the [Blender_%s_RenderExecutable] render executable list." % version )

        if executableList == "":
            executableList = ( self.GetConfigEntryWithDefault( "Blender_RenderExecutable", "" ) or "" ).strip()
            if version != "":
                self.LogInfo( "Using the [Blender_RenderExecutable] template for Blender %s." % version )

        executableList = self.ExpandVersionPlaceholders( executableList, version, versionFull )

        executable = ""
        if SystemUtils.IsRunningOnWindows():
            if build == "32bit":
                self.LogInfo( "Enforcing 32 bit build of Blender" )
                executable = FileUtils.SearchFileListFor32Bit( executableList )
                if executable == "":
                    self.LogWarning( "32 bit Blender render executable was not found in the semicolon separated list \"" + executableList + "\". Checking for any executable that exists instead." )

            elif build == "64bit":
                self.LogInfo( "Enforcing 64 bit build of Blender" )
                executable = FileUtils.SearchFileListFor64Bit( executableList )
                if executable == "":
                    self.LogWarning( "64 bit Blender render executable was not found in the semicolon separated list \"" + executableList + "\". Checking for any executable that exists instead." )

        if executable == "":
            self.LogInfo( "Not enforcing a build of Blender" )
            executable = FileUtils.SearchFileList( executableList )
            if executable == "":
                self.LogWarning( "Blender render executable was not found in the semicolon separated list \"" + executableList + "\". Falling back to the stock Deadline render executable lookup." )
                try:
                    executable = self.GetRenderExecutable( "Blender_RenderExecutable", "Blender" )
                except Exception:
                    executable = ""

        if executable == "":
            self.FailRender( "Blender render executable was not found in the semicolon separated list \"" + executableList + "\". The path to the render executable can be configured from the Plugin Configuration in the Deadline Monitor (Configure Plugins -> Blender -> Render Executables)." )

        return executable

    ########################################################################
    ## Render script resolution
    ########################################################################

    def GetRepositoryFilePathSafe( self, relativePath, checkCustom ):
        """RepositoryUtils.GetRepositoryFilePath with a defensive wrapper."""
        try:
            return RepositoryUtils.GetRepositoryFilePath( relativePath, checkCustom )
        except Exception:
            return ""

    def GetRepositoryRootSafe( self ):
        try:
            return RepositoryUtils.GetRootDirectory()
        except Exception:
            return ""

    def ResolveRenderScriptPath( self ):
        """Return the full path of BlenderRenderScript.py.

        Lookup order:
          1. the [Blender_RenderScriptPath] plugin configuration entry (absolute path);
          2. <repository>/custom/plugins/Blender/BlenderRenderScript.py (GetRepositoryFilePath with custom=True);
          3. the factory copy (GetRepositoryFilePath with custom=False);
          4. <repository root>/custom/plugins/Blender/BlenderRenderScript.py.

        Fails the render with an actionable message when none of them exist, so a
        broken deployment never silently renders with the wrong settings.
        """
        candidates = []

        configured = self.GetConfigEntryWithDefault( "Blender_RenderScriptPath", "" ).strip()
        if configured != "":
            candidates.append( configured )

        candidates.append( self.GetRepositoryFilePathSafe( RENDER_SCRIPT_RELATIVE_PATH, True ) )
        candidates.append( self.GetRepositoryFilePathSafe( RENDER_SCRIPT_RELATIVE_PATH, False ) )

        root = self.GetRepositoryRootSafe()
        if root is not None and str( root ) != "":
            candidates.append( os.path.join( str( root ), "custom", "plugins", "Blender", "BlenderRenderScript.py" ) )

        for candidate in candidates:
            if candidate is not None and str( candidate ) != "" and File.Exists( str( candidate ) ):
                return str( candidate )

        self.FailRender( "Could not find BlenderRenderScript.py. Looked in: " + " | ".join( [ str( c ) for c in candidates ] ) + ". Set the [Blender_RenderScriptPath] entry in the Blender plugin configuration to the full path of the script." )
        return ""

    ########################################################################
    ## Render arguments
    ########################################################################

    def SetRenderEnvironment( self, name, value ):
        """Set a DLB_* variable through both APIs Deadline offers.

        The shipped simple plugins use one or the other (MayaCmd uses
        SetEnvironmentVariable, DraftPlugin uses SetProcessEnvironmentVariable). Neither was
        seen to reach Blender in this deployment, which is why the command line carries the
        options as well; this stays as a second channel. Every variable is set, including the
        empty ones, so a value cannot leak into the next task on the same Worker.
        """
        for setterName in ( "SetEnvironmentVariable", "SetProcessEnvironmentVariable" ):
            setter = getattr( self, setterName, None )
            if setter is None:
                continue
            try:
                setter( name, value )
            except Exception as error:
                self.LogWarning( "%s( %s ) failed: %s" % ( setterName, name, error ) )

    def QuoteOptionToken( self, option, value ):
        """Build one "option=value" token, quoted only when the command line needs it."""
        text = str( value )

        if '"' in text:
            self.FailRender( "The value of '%s' contains a double quote, which cannot be passed to Blender: %s" % ( option, text ) )

        if " " in text or "\t" in text:
            if SystemUtils.IsRunningOnWindows() and text.endswith( "\\" ):
                # A backslash directly before the closing quote would escape it on Windows.
                text = text + "\\"
            return '"%s=%s"' % ( option, text )

        return "%s=%s" % ( option, text )

    def BuildRenderOptionTokens( self, outputFile, startFrame, endFrame ):
        """Return the "option=value" tokens passed to Blender after "--".

        Blender hands everything after "--" to the script untouched in sys.argv, so this
        transport works regardless of the environment and of Blender's argument passes (see
        the comment in RenderArgument).
        """
        tokens = []
        summary = []

        for pluginInfoKey, option, envName in RENDER_OPTION_KEYS:
            value = self.GetPluginInfoEntryWithDefault( pluginInfoKey, "" ).strip()
            self.SetRenderEnvironment( envName, "" if value in UNSET_VALUES else value )
            if value in UNSET_VALUES:
                continue
            tokens.append( self.QuoteOptionToken( option, value ) )
            summary.append( "%s=%s" % ( option, value ) )

        markers = "1" if self.GetBooleanPluginInfoEntryWithDefault( "MarkerOverride", False ) else ""
        self.SetRenderEnvironment( "DLB_MARKER_OVERRIDE", markers )
        if markers != "":
            tokens.append( self.QuoteOptionToken( "markers", markers ) )
            summary.append( "markers=1" )

        threads = self.GetPluginInfoEntryWithDefault( "Threads", "0" ).strip()
        self.SetRenderEnvironment( "DLB_THREADS", threads )
        if threads != "":
            tokens.append( self.QuoteOptionToken( "threads", threads ) )
            summary.append( "threads=%s" % threads )

        if outputFile != "":
            self.SetRenderEnvironment( "DLB_OUTPUT", outputFile )
            tokens.append( self.QuoteOptionToken( "output", outputFile ) )
            summary.append( "output=%s" % outputFile )

        if startFrame > 0 and endFrame > 0:
            self.SetRenderEnvironment( "DLB_FRAME_START", str( startFrame ) )
            self.SetRenderEnvironment( "DLB_FRAME_END", str( endFrame ) )
            tokens.append( self.QuoteOptionToken( "frames", "%s-%s" % ( startFrame, endFrame ) ) )
            summary.append( "frames=%s-%s" % ( startFrame, endFrame ) )

        # Always present: the script refuses to finish silently when it is missing.
        self.SetRenderEnvironment( "DLB_RENDER", "1" )
        tokens.append( self.QuoteOptionToken( "render", "1" ) )

        if summary:
            self.LogInfo( "Blender render options: " + ", ".join( summary ) )
        else:
            self.LogInfo( "Blender render options: none (rendering with the settings stored in the .blend file)." )
        self.LogInfo( "Blender render option tokens: " + " ".join( tokens ) )

        return tokens

    def RenderArgument(self):
        sceneFile = self.GetPluginInfoEntryWithDefault( "SceneFile", self.GetDataFilename() )
        sceneFile = RepositoryUtils.CheckPathMapping( sceneFile )
        if SystemUtils.IsRunningOnWindows():
            sceneFile = sceneFile.replace( "/", "\\" )
            if sceneFile.startswith( "\\" ) and not sceneFile.startswith( "\\\\" ):
                sceneFile = "\\" + sceneFile
        else:
            sceneFile = sceneFile.replace( "\\", "/" )

        outputFile = self.GetPluginInfoEntryWithDefault( "OutputFile", "" )
        outputFile = RepositoryUtils.CheckPathMapping( outputFile )

        renderScript = self.ResolveRenderScriptPath()

        startFrame = self.GetStartFrame()
        endFrame = self.GetEndFrame()
        tokens = self.BuildRenderOptionTokens( outputFile, startFrame, endFrame )

        # Blender does NOT process its command line strictly left to right: the arguments are
        # handled in passes (see ARG_PASS_* in creator_intern.h) and the render flags ("-a",
        # "-f", "-o", "-x", "-s", "-e") are handled in an earlier pass than "--python". A
        # script placed before "-a" therefore runs *after* the render has already finished,
        # which is why the script applies the settings and triggers the render itself.
        #
        # "-b <file>" loads the .blend in that earlier pass, so by the time the script runs,
        # bpy.context.scene is the scene that was submitted.
        #
        # The render options travel after "--" because Blender hands everything after it to
        # the script in sys.argv; that works no matter how the passes are ordered, and no
        # matter whether the DLB_* environment variables survive into the Blender process
        # (they did not on this farm).
        renderArgument = " -b \"" + sceneFile + "\""
        renderArgument += " -t " + self.GetPluginInfoEntryWithDefault( "Threads", "0" )
        renderArgument += " --python-exit-code 1"
        renderArgument += " --python \"" + renderScript + "\""
        if tokens:
            renderArgument += " -- " + " ".join( tokens )

        self.LogInfo( "Blender command line: blender" + renderArgument )

        return renderArgument

    ########################################################################
    ## Progress / errors
    ########################################################################

    def PreRenderTasks(self):
        self.LogInfo( "Blender job starting..." )

        # Plugin specific values for progress
        self.totalFrames = self.GetEndFrame() - self.GetStartFrame() + 1
        self.finishedFrames = 0
        self.totalChunks = 0
        self.currentChunk = 0
        self.chunkType = ""

        self.UpdateProgress()

    def PostRenderTasks(self):
        self.LogInfo( "Blender job finished." )

    def UpdateProgress(self):
        progress = self.finishedFrames

        # If we know the chunk type, we should have set its progress as well
        if self.chunkType != "":
            progress += (self.currentChunk / float(self.totalChunks))
            message = "Rendering %(ct)s %(cc)s/%(tt)s of frame %(ff)s/%(tf)s for this task"
        else:
            message = "Rendering frame %(ff)s/%(tf)s for this task"

        self.SetStatusMessage( message % {
            "ct": self.chunkType,
            "ff": str(self.finishedFrames + 1),
            "tf": str(self.totalFrames),
            "cc": str(self.currentChunk),
            "tt": str(self.totalChunks) })

        progress = progress / float( self.totalFrames )
        self.SetProgress( progress * 100 )

        # The stock plugin read the job-level "SupressOutput" key (a typo) while the
        # configuration entry is named "SuppressOutput", so the setting never had an
        # effect. The job-level key is still honoured when present, but the plugin
        # configuration entry now works as documented.
        if self.GetBooleanPluginInfoEntryWithDefault( "SupressOutput", self.GetBooleanConfigEntryWithDefault( "SuppressOutput", True ) ):
            self.SuppressThisLine()

    def HandleStdoutSaved(self):
        self.finishedFrames += 1
        self.currentChunk = 0 # Avoid incorrect progress math after addtion

        self.UpdateProgress()

        if self.finishedFrames + 1 > self.totalFrames:
            # This avoids us showing a status message of "rendering frame 2/1"
            self.SetStatusMessage( "Task complete." )

    def HandleTileProgress(self):
        ''' Find tile progress for Cycle's tile render '''
        self.currentChunk = int( self.GetRegexMatch(1) )
        self.totalChunks  = int( self.GetRegexMatch(2) )
        self.chunkType = "tile"
        self.UpdateProgress()

    def HandleSampleProgress(self):
        ''' Find sample progress for Cycle's progressive render and for EEVEE '''
        # Samples are reported in order, so let's be awesome
        self.currentChunk = int( self.GetRegexMatch(1) )
        self.totalChunks  = int( self.GetRegexMatch(2) )
        self.chunkType = "sample"
        self.UpdateProgress()

    def HandleSceneProgress(self):
        ''' Find sub-frame progress for the Blender Internal renderer '''
        # We hit problems with things like motion blur and sub-surf sampling
        # when reporting progress since lists progress multiple times without
        # always telling us how many loops there would be.

        # Tiles aren't reported in order, so let's track it ourselves
        # self.currentChunk += 1
        # self.totalChunks  = int( self.GetRegexMatch(2) )
        # self.chunkType = "chunk"
        self.UpdateProgress()

    def HandleStdoutError(self):
        if not self.GetBooleanPluginInfoEntryWithDefault( "StrictErrorChecking", False ):
            return
        self.FailRender( "Strict error checking is enabled and Blender reported: " + self.GetRegexMatch(0) )

    def HandleStdoutFailed(self):
        self.FailRender( self.GetRegexMatch(0) )
