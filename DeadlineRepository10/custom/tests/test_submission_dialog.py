"""Build the real "Submit Blender Job To Deadline" dialog and submit through it.

Runs under Deadline's own Python, because it uses the actual DeadlineScriptDialog control
API, the actual PyQt5 combination tree / options table and the actual job/plugin file
writing (System.IO.StreamWriter). Only the side effects are replaced: the dialog is never
shown, the right-click menu is never executed, and the call that would submit to the farm
records its arguments instead.

    deadlinecommand -ExecuteScript "<DeadlineRepository>\\custom\\tests\\test_submission_dialog.py"
"""

import importlib.util
import os
import re
import shutil
import sys
import tempfile
import time
import types
import traceback

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


def LoadSubmissionModule(work):
    """Load the submission script with the real Deadline/Qt modules, minus the side effects."""
    spec = importlib.util.spec_from_file_location("blender_submission", SUBMISSION_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    commands = []
    messages = []
    shown = []

    module.ClientUtils = types.SimpleNamespace(
        GetDeadlineTempPath=lambda: work,
        GetUsersSettingsDirectory=lambda: work,
        ExecuteCommandAndGetOutput=lambda args: commands.append(list(args)) or "Submitted",
    )
    module.PathUtils = types.SimpleNamespace(IsPathLocal=lambda path: False)
    module.GetSettingsFilename = lambda: os.path.join(work, "BlenderSettings.ini")

    # The integration tabs are not part of what this test checks.
    module.IntegrationUI = types.SimpleNamespace(
        IntegrationDialog=lambda: types.SimpleNamespace(
            AddIntegrationTabs=lambda *args, **kwargs: None,
            CloseProjectManagementConnections=lambda *args: None,
            CheckIntegrationSanity=lambda outputFile: True,
            IntegrationProcessingRequested=lambda: False,
            IntegrationGroupBatchRequested=lambda: False,
            WriteIntegrationInfo=lambda writer, index: index,
        )
    )

    realDialog = module.DeadlineScriptDialog

    class RecordingDialog(realDialog):
        def ShowDialog(self, *args, **kwargs):
            shown.append(True)
            return True

        def ShowMessageBox(self, message, title="", buttons=None):
            messages.append((title, message))
            return "Yes"

    module.DeadlineScriptDialog = RecordingDialog

    # The confirmation before submitting is a real dialog now: answer it from here, once for the
    # whole test, so no modal window is opened and the calls can be checked.
    confirmations = []
    module.ConfirmSubmission = lambda jobs, values: confirmations.append(len(jobs)) or True
    module.testConfirmations = confirmations

    return module, commands, messages, shown


def CheckedLeafTexts(tree):
    from PyQt5 import QtCore

    checked = []
    for sceneIndex in range(tree.topLevelItemCount()):
        sceneItem = tree.topLevelItem(sceneIndex)
        for layerIndex in range(sceneItem.childCount()):
            layerItem = sceneItem.child(layerIndex)
            for cameraIndex in range(layerItem.childCount()):
                cameraItem = layerItem.child(cameraIndex)
                if cameraItem.checkState(0) == QtCore.Qt.Checked:
                    checked.append("%s/%s/%s" % (sceneItem.text(0), layerItem.text(0), cameraItem.text(0)))
    return checked


def TickItem(item):
    """Tick an item the way a user would click its box."""
    from PyQt5 import QtCore

    item.setCheckState(0, QtCore.Qt.Checked)


def RunMenuEntry(module, item, label):
    for entryLabel, handler in module.CombinationTreeMenuEntries(item):
        if entryLabel == label:
            handler()
            module.CombinationSelectionChanged()
            return True
    return False


def MenuLabels(module, item):
    return [label for label, handler in module.CombinationTreeMenuEntries(item)]


def RowWidget(module, row, key):
    """The editor of one row's column, by setting name."""
    return module.combinationRows[row]["widgets"][key]


def SelectCells(module, cells):
    """Select cells (row, key) the way a click with Ctrl would."""
    from PyQt5 import QtCore
    table = module.combinationTable
    selection = table.selectionModel()
    selection.clearSelection()
    for row, key in cells:
        selection.select(table.model().index(row, module.WIDGET_COLUMNS[key]),
                         QtCore.QItemSelectionModel.Select)


def RowWidgets(module, row):
    """The editors of a row, in column order: output, frames, chunk, engine, format, gpu,
    res x, res y, strict, markers.

    Taken from the row entry rather than from the cells: some cells hold a container with a
    selection strip next to the editor.
    """
    return tuple(RowWidget(module, row, key) for key in module.SETTING_KEYS)


def OutputBrowseButton(module, row):
    """The 'choose a path' button next to a row's output file."""
    from PyQt5 import QtWidgets
    cell = module.combinationTable.cellWidget(row, 1)
    if cell is None:
        return None
    return cell.findChild(QtWidgets.QPushButton)


def RowForLabel(module, label):
    """The row index whose combination column ends with this label.

    The column reads "Scene / View Layer / Camera" now, so matching by suffix keeps the callers
    that only care about the view layer and camera working.
    """
    table = module.combinationTable
    for row in range(table.rowCount()):
        if table.item(row, 0).text().endswith(label) or table.item(row, 0).text() == label:
            return row
    return -1


def __main__():
    work = tempfile.mkdtemp(prefix="dlb_submit_dialog_test_")
    try:
        sceneFile = os.path.join(work, "scene.blend")
        with open(sceneFile, "w") as handle:
            handle.write("not a real blend file, only its existence is checked")
        outputDirectory = os.path.join(work, "render")
        os.makedirs(outputDirectory)

        print("submission script: %s" % SUBMISSION_SCRIPT)
        module, commands, messages, shown = LoadSubmissionModule(work)

        # Exactly what the Blender-side proxy sends: the context blob as the 6th argument,
        # base64 encoded JSON.
        import base64
        import json

        context = {
            "active_scene": "Scene_Main",
            "scenes": ["Scene_Main", "Scene_Alt"],
            "view_layers": ["Beauty", "Mask"],
            "cameras": ["ShotCam_0", "ShotCam_1"],
            "active_camera": "ShotCam_1",
            "active_view_layer": "Beauty",
            "engine": "BLENDER_EEVEE",
            "image_format": "PNG",
            "gpu_device": "NONE",
            "resolution_x": 1920,
            "resolution_y": 1080,
            "scene_details": {
                "Scene_Main": {"view_layers": ["Beauty", "Mask"], "cameras": ["ShotCam_0", "ShotCam_1"],
                               "engine": "BLENDER_EEVEE", "image_format": "PNG",
                               "resolution_x": 1920, "resolution_y": 1080},
                "Scene_Alt": {"view_layers": ["Layers_A"], "cameras": ["AltCam"],
                              "engine": "CYCLES", "image_format": "OPEN_EXR",
                              "resolution_x": 1280, "resolution_y": 720},
            },
            "version": "5.2",
            "version_full": "5.2.2",
        }

        print("")
        print("the dialog builds with the tree and the options table")
        module.__main__(
            sceneFile,
            "1-10",
            os.path.join(outputDirectory, "beauty_####.png"),
            "0",
            "64bit",
            base64.b64encode(json.dumps(context).encode("utf-8")).decode("ascii"),
        )
        check(bool(shown), "the dialog was shown (ShowDialog was called)")
        check(not [message for message in messages if message[0] == "Error"],
              "no error dialog while building it (%s)" % [title for title, _ in messages])
        check(module.submitContext.get("active_camera") == "ShotCam_1",
              "the context was decoded from the command line")

        for control in ("ChainJobsBox", "TreeHintBox"):
            try:
                module.scriptDialog.GetValue(control)
                check(True, "%s exists" % control)
            except Exception as error:
                check(False, "%s exists: %s" % (control, error))

        for control in ("UseTreeBox", "EngineBox", "FormatBox", "GpuBox", "ResolutionXBox",
                        "ResolutionYBox", "StrictErrorBox", "MarkerOverrideBox",
                        "SceneNameBox", "ViewLayerBox", "CameraBox",
                        "OutputBox", "FramesBox", "ChunkSizeBox", "CopyOptionsButton"):
            try:
                module.scriptDialog.GetValue(control)
                check(False, "%s is gone" % control)
            except Exception:
                check(True, "%s is gone" % control)

        print("")
        print("the tree lists every scene, with the active layer and camera of the active scene ticked")
        tree = module.combinationTree
        check(tree is not None, "the tree was created")
        check(tree.topLevelItemCount() == 2, "two scenes at the top (%d)" % tree.topLevelItemCount())
        sceneItem = tree.topLevelItem(0)
        otherSceneItem = tree.topLevelItem(1)
        check(sceneItem.text(0) == "Scene_Main" and otherSceneItem.text(0) == "Scene_Alt",
              "the scenes the file has: %s" % [sceneItem.text(0), otherSceneItem.text(0)])
        check([sceneItem.child(index).text(0) for index in range(sceneItem.childCount())]
              == ["Beauty", "Mask"], "the view layers of the first scene are its children")
        check([otherSceneItem.child(index).text(0) for index in range(otherSceneItem.childCount())]
              == ["Layers_A"], "the second scene brings its own view layers")
        check([sceneItem.child(0).child(index).text(0) for index in range(sceneItem.child(0).childCount())]
              == ["ShotCam_0", "ShotCam_1"], "the cameras are the leaves")
        check([otherSceneItem.child(0).child(index).text(0) for index in range(otherSceneItem.child(0).childCount())]
              == ["AltCam"], "and so does every other scene")
        check(CheckedLeafTexts(tree) == ["Scene_Main/Beauty/ShotCam_1"],
              "only the active scene's active layer and camera start ticked: %s" % CheckedLeafTexts(tree))
        check("1 of 5 combinations ticked" in str(module.scriptDialog.GetValue("TreeHintBox")),
              "the hint counts every scene: %r" % module.scriptDialog.GetValue("TreeHintBox"))

        from PyQt5 import QtCore, QtWidgets

        check(sceneItem.child(0).checkState(0) == QtCore.Qt.PartiallyChecked,
              "the view layer above the ticked camera is partially checked (%s)"
              % sceneItem.child(0).checkState(0))
        check(sceneItem.child(1).checkState(0) == QtCore.Qt.Unchecked,
              "a view layer with nothing ticked under it is unchecked")
        check(sceneItem.checkState(0) == QtCore.Qt.PartiallyChecked,
              "and the scene above them is partially checked (%s)" % sceneItem.checkState(0))
        check(otherSceneItem.checkState(0) == QtCore.Qt.Unchecked,
              "a scene with nothing ticked is unchecked")

        print("")
        print("the options table has one row per ticked combination")
        table = module.combinationTable
        check(table is not None, "the table was created")
        check(table.rowCount() == 1, "one row for one ticked combination (%d)" % table.rowCount())
        check(table.columnCount() == len(module.TABLE_HEADERS),
              "the table has the option columns (%d)" % table.columnCount())
        check(table.item(0, 0).text() == "Scene_Main / Beauty / ShotCam_1",
              "the row names scene, view layer and camera: %r" % table.item(0, 0).text())

        output, frames, chunkSize, engine, imageFormat, gpuDevice, resolutionX, resolutionY, strictError, markerOverride = RowWidgets(module, 0)
        check(output.text() == os.path.join(outputDirectory, "beauty_####.png"),
              "the output path of the submission is the row's default: %r" % output.text())
        check(frames.text() == "1-10", "the frame list of the submission is the row's default: %r" % frames.text())
        check(chunkSize.value() == 1, "frames per task starts at 1")
        check(resolutionX.value() == 1920 and resolutionY.value() == 1080,
              "the resolution default is the one the row's scene has (%d x %d)"
              % (resolutionX.value(), resolutionY.value()))
        check(engine.currentText() == "Use Scene Setting" and imageFormat.currentText() == "Use Scene Setting",
              "the render options start at 'Use Scene Setting'")
        check(not strictError.isChecked() and not markerOverride.isChecked(), "and both switches are off")

        print("")
        print("a combination of another scene gets that scene's resolution")
        TickItem(otherSceneItem.child(0).child(0))          # Scene_Alt / Layers_A / AltCam
        module.CombinationSelectionChanged()
        otherRow = RowForLabel(module, "Layers_A / AltCam")
        check(otherRow >= 0, "the other scene's combination has a row (%d)" % otherRow)
        if otherRow >= 0:
            otherWidgets = RowWidgets(module, otherRow)
            check(otherWidgets[6].value() == 1280 and otherWidgets[7].value() == 720,
                  "its resolution is Scene_Alt's (%d x %d)"
                  % (otherWidgets[6].value(), otherWidgets[7].value()))
        RunMenuEntry(module, otherSceneItem, "Select None")
        module.CombinationSelectionChanged()

        print("")
        print("ticking more combinations adds rows and keeps the settings already made")
        # Re-read the widgets: the rows were rebuilt when the other scene was ticked.
        keptRow = RowForLabel(module, "Beauty / ShotCam_1")
        RowWidgets(module, keptRow)[3].setCurrentText("cycles")
        RowWidgets(module, keptRow)[6].setValue(800)
        TickItem(sceneItem)                                  # select everything in the scene
        check(len(CheckedLeafTexts(tree)) == 4, "all four leaves are ticked now")
        check(table.rowCount() == 4, "the table has four rows now (%d)" % table.rowCount())
        kept = RowForLabel(module, "Beauty / ShotCam_1")
        check(kept >= 0, "the row of the combination that had settings is still there")
        check(RowWidgets(module, kept)[3].currentText() == "cycles",
              "the settings stayed with their own combination (engine)")
        check(RowWidgets(module, kept)[6].value() == 800,
              "the settings stayed with their own combination (resolution)")
        check(RowWidgets(module, RowForLabel(module, "Beauty / ShotCam_0"))[3].currentText()
              == "Use Scene Setting", "a newly added row starts from the defaults")

        print("")
        print("right-click a row: back to the defaults")
        module.ResetRowsToDefaults([kept])
        keptWidgets = RowWidgets(module, kept)
        check(keptWidgets[3].currentText() == "Use Scene Setting", "the engine is back to 'Use Scene Setting'")
        check(keptWidgets[6].value() == 1920 and keptWidgets[7].value() == 1080,
              "the resolution is back to the scene's (%d x %d)" % (keptWidgets[6].value(), keptWidgets[7].value()))
        check(keptWidgets[0].text() == os.path.join(outputDirectory, "beauty_####.png"),
              "and the output path is the default again: %r" % keptWidgets[0].text())

        print("")
        print("the table's right-click menu")
        menu = module.BuildCombinationTableMenu(0, 4)
        menuLabels = [action.text() for action in menu.actions()]
        check("Reset Selected Cells To Defaults" in menuLabels,
              "the per-cell reset entry is there: %s" % menuLabels)
        check("Reset Selected Rows To Defaults" in menuLabels, "and the per-row one")
        check("Reset All Rows To Defaults" in menuLabels, "and the one for every row")
        check("Copy First Row To All" in menuLabels, "and the copy entry")
        check(not any("preset" in label.lower() for label in menuLabels),
              "but no output presets outside the output column: %s" % menuLabels)

        print("")
        print("the right-click menu of an output path cell")
        pathMenu = module.BuildCombinationTableMenu(0, 1, module.combinationRows[0]["widgets"]["output"])
        pathLabels = [action.text() for action in pathMenu.actions()]
        check("Output File presets" in pathLabels, "the presets are on the path itself: %s" % pathLabels)
        pathPresets = [action.text() for action in pathMenu.actions()
                       if action.menu() is not None for action in action.menu().actions()]
        check(any("{DEFAULT_PATH}" in label for label in pathPresets),
              "with the patterns in their placeholders: %s" % pathPresets)
        check("Copy" in pathLabels and "Paste" in pathLabels,
              "and the usual text editing entries: %s" % pathLabels)

        print("")
        print("selecting and changing cells")
        check(module.combinationTable.selectionBehavior()
              == QtWidgets.QAbstractItemView.SelectItems,
              "single cells can be selected, not only whole rows")
        check(module.combinationTable.selectionMode()
              == QtWidgets.QAbstractItemView.ExtendedSelection,
              "and more than one with Ctrl / Shift")

        # Two rows: give them a frame list and an engine, then edit one cell of each column.
        RunMenuEntry(module, sceneItem, "Select All")
        module.CombinationSelectionChanged()
        rowA = RowForLabel(module, "Beauty / ShotCam_0")
        rowB = RowForLabel(module, "Beauty / ShotCam_1")
        check(rowA >= 0 and rowB >= 0 and rowA != rowB, "two rows to work with (%d, %d)" % (rowA, rowB))

        module.ResetRowsToDefaults([rowA, rowB])
        framesColumn = module.WIDGET_COLUMNS["frames"]
        SelectCells(module, [(rowA, "frames"), (rowB, "frames")])
        check(sorted(row for row, key in module.SelectedCells() if key == "frames") == sorted([rowA, rowB]),
              "both frame list cells are selected: %s" % module.SelectedCells())

        RowWidgets(module, rowA)[1].setText("1-42")
        RowWidgets(module, rowA)[1].editingFinished.emit()
        check(RowWidgets(module, rowB)[1].text() == "1-42",
              "editing one frame list changed the other selected one too: %r"
              % RowWidgets(module, rowB)[1].text())
        check(RowWidgets(module, rowB)[3].currentText() == "Use Scene Setting"
              and RowWidgets(module, rowB)[6].value() == 1920,
              "and left the other columns alone")

        engineColumn = module.WIDGET_COLUMNS["engine"]
        SelectCells(module, [(rowA, "engine"), (rowB, "engine")])
        RowWidgets(module, rowA)[3].setCurrentText("cycles")
        check(RowWidgets(module, rowB)[3].currentText() == "cycles",
              "a drop-down change is copied as well: %r" % RowWidgets(module, rowB)[3].currentText())

        resolutionColumn = module.WIDGET_COLUMNS["resolution_x"]
        SelectCells(module, [(rowA, "resolution_x"), (rowB, "resolution_x")])
        RowWidgets(module, rowA)[6].setValue(640)
        check(RowWidgets(module, rowB)[6].value() == 640,
              "a resolution changed in one cell reaches the other selected cell: %d"
              % RowWidgets(module, rowB)[6].value())

        strictColumn = module.WIDGET_COLUMNS["strict_error"]
        SelectCells(module, [(rowA, "strict_error"), (rowB, "strict_error")])
        RowWidgets(module, rowA)[8].setChecked(True)
        check(RowWidgets(module, rowB)[8].isChecked(),
              "and so does a check box")

        print("")
        print("clicking an editor selects its cell")
        # The cells hold real widgets, so a click on the editor never reaches the table; the
        # filter on every editor - and on the parts inside it - makes the cell selectable.
        from PyQt5 import QtTest

        SelectCells(module, [])
        check(module.SelectedCells() == [], "nothing is selected to start with")

        QtTest.QTest.mouseClick(RowWidget(module, rowA, "frames"), QtCore.Qt.LeftButton)
        check(module.SelectedCells() == [(rowA, "frames")],
              "clicking the frame list selected its cell: %s" % module.SelectedCells())

        spinInner = RowWidget(module, rowB, "chunk_size").findChild(QtWidgets.QLineEdit)
        check(spinInner is not None, "the spin box has an inner line edit to click")
        if spinInner is not None:
            QtTest.QTest.mouseClick(spinInner, QtCore.Qt.LeftButton)
            check(module.SelectedCells() == [(rowB, "chunk_size")],
                  "clicking inside 'frames per task' selected that cell: %s" % module.SelectedCells())

        QtTest.QTest.mouseClick(RowWidget(module, rowB, "strict_error"), QtCore.Qt.LeftButton)
        check(module.SelectedCells() == [(rowB, "strict_error")],
              "and a check box selects its cell as well: %s" % module.SelectedCells())
        print("")
        print("a click selects the cell and lets the editor open")
        # Reverted to the plain behaviour: a click on a cell selects it and the editor handles
        # the click itself, so a text field is ready to be typed into straight away.
        from PyQt5 import QtGui, QtTest as QtTestModule

        SelectCells(module, [])
        framesCell = RowWidget(module, rowA, "frames")
        check(not framesCell.isReadOnly(), "a text field is editable, not read-only")

        QtTest.QTest.mouseClick(framesCell, QtCore.Qt.LeftButton)
        check(module.SelectedCells() == [(rowA, "frames")],
              "clicking it selected its cell: %s" % module.SelectedCells())

        before = framesCell.text()
        framesCell.setFocus()
        QtTestModule.QTest.keyClick(framesCell, QtCore.Qt.Key_End)
        QtTestModule.QTest.keyClick(framesCell, QtCore.Qt.Key_X)
        check(framesCell.text().lower() == (before + "x").lower(),
              "and it takes typing straight away: %r" % framesCell.text())
        framesCell.setText(before)

        QtTest.QTest.mouseClick(RowWidget(module, rowB, "chunk_size").findChild(QtWidgets.QLineEdit)
                                or RowWidget(module, rowB, "chunk_size"), QtCore.Qt.LeftButton)
        check(module.SelectedCells() == [(rowB, "chunk_size")],
              "clicking inside another editor selects that cell: %s" % module.SelectedCells())

        print("")
        print("a drop-down has a blank strip on its left that only selects")
        engineCell = module.combinationTable.cellWidget(0, module.WIDGET_COLUMNS["engine"])
        strip = module.combinationTable.cellWidget(0, module.WIDGET_COLUMNS["engine"]).layout().itemAt(0).widget()
        check(strip is not None and strip.width() > 0,
              "the strip is the first widget of the cell (%r)" % strip)
        check(module.CellForWidget(strip) == (0, module.WIDGET_COLUMNS["engine"], "engine"),
              "and it belongs to the engine cell: %s" % (module.CellForWidget(strip),))
        check(engineCell.layout().itemAt(1).widget() is RowWidget(module, 0, "engine"),
              "with the drop-down next to it")

        SelectCells(module, [])
        stripEvent = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(2, 2),
                                       QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                                       QtCore.Qt.NoModifier)
        handled = module.HandleCellEvent(strip, stripEvent)
        check(handled is True and module.SelectedCells() == [(0, "engine")],
              "clicking the strip selects the cell and swallows the press: %s"
              % module.SelectedCells())

        # The strip has to work with Ctrl as well: the press must not travel on to the table,
        # where a Ctrl click on a selected cell means "remove it again".
        SelectCells(module, [(0, "frames")])
        ctrlEvent = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(2, 2),
                                      QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                                      QtCore.Qt.ControlModifier)
        module.HandleCellEvent(strip, ctrlEvent)
        check(sorted(module.SelectedCells()) == sorted([(0, "engine"), (0, "frames")]),
              "a Ctrl click on the strip adds the cell instead of removing it: %s"
              % module.SelectedCells())
        SelectCells(module, [])
        combo = RowWidget(module, 0, "engine")
        check(not combo.view().isVisible(), "and does not open the list")
        check(not combo.hasFocus(), "and does not put the caret into the drop-down")

        # The blank padding of a container behaves the same way: it selects and is swallowed.
        SelectCells(module, [])
        outputCell = module.combinationTable.cellWidget(0, module.WIDGET_COLUMNS["output"])
        containerEvent = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(1, 1),
                                           QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                                           QtCore.Qt.ControlModifier)
        check(module.HandleCellEvent(outputCell, containerEvent) is True,
              "a Ctrl click on the padding of the output cell is swallowed")
        check(module.SelectedCells() == [(0, "output")],
              "and it selects that cell: %s" % module.SelectedCells())
        SelectCells(module, [])

        for key in ("image_format", "gpu_device"):
            cell = module.combinationTable.cellWidget(0, module.WIDGET_COLUMNS[key])
            item = cell.layout().itemAt(0).widget()
            check(module.CellForWidget(item) == (0, module.WIDGET_COLUMNS[key], key),
                  "the %s cell has one as well" % key)

        print("")
        print("the output cell keeps its browse button when the column is narrow")
        from PyQt5 import QtWidgets as QtWidgetsModule

        outputCell = module.combinationTable.cellWidget(0, module.WIDGET_COLUMNS["output"])
        layout = outputCell.layout()
        pathField = module.combinationRows[0]["widgets"]["output"]
        check(pathField.minimumWidth() == 0
              and pathField.sizePolicy().horizontalPolicy() == QtWidgetsModule.QSizePolicy.Ignored,
              "the path field shrinks instead of pushing the button out")
        check(layout.itemAt(0).widget() is pathField and layout.itemAt(1).widget() is
              OutputBrowseButton(module, 0),
              "and the button is the last item, pinned to the right edge")
        check(layout.stretch(1) == 0 and layout.stretch(0) == 1,
              "with all the spare width going to the path field")

        widths = [module.combinationTable.columnWidth(module.WIDGET_COLUMNS[key])
                  for key in ("engine", "image_format", "gpu_device")]
        check(widths[0] == widths[2],
              "the engine and gpu columns are equally wide: %s" % widths)
        check(widths[1] > widths[0],
              "image format is wider, so 'Use Scene Setting' fits next to its strip: %s" % widths)

        # The drop-downs must not ask for more width than their column: that is what made one
        # reach over the strip of the next one.
        from PyQt5 import QtWidgets as QtWidgetsModule

        for key in ("engine", "image_format", "gpu_device"):
            combo = module.combinationRows[0]["widgets"][key]
            check(combo.sizeAdjustPolicy()
                  == QtWidgetsModule.QComboBox.AdjustToMinimumContentsLengthWithIcon,
                  "the %s drop-down does not demand a width of its own" % key)
            check(combo.minimumSizeHint().width() <= module.combinationTable.columnWidth(
                      module.WIDGET_COLUMNS[key]),
                  "and fits into its column (%d <= %d)"
                  % (combo.minimumSizeHint().width(),
                     module.combinationTable.columnWidth(module.WIDGET_COLUMNS[key])))

        print("")
        print("the window does not grow when more combinations are ticked")
        # Fixed column widths: a longer name, a longer path or a second scene must not resize the
        # dialog. (The "Will submit" line that used to grow with the text is gone entirely.)
        RunMenuEntry(module, sceneItem, "Clear The Whole Tree")
        TickItem(sceneItem.child(0).child(0))
        module.CombinationSelectionChanged()
        check(module.combinationTable.rowCount() == 1, "one row to start with")
        widthsBefore = [module.combinationTable.columnWidth(column)
                        for column in range(module.combinationTable.columnCount())]

        RunMenuEntry(module, sceneItem, "Select All")
        module.CombinationSelectionChanged()
        check(module.combinationTable.rowCount() > 1,
              "several rows are there now (%d)" % module.combinationTable.rowCount())
        widthsAfter = [module.combinationTable.columnWidth(column)
                       for column in range(module.combinationTable.columnCount())]
        check(widthsAfter == widthsBefore,
              "the columns kept their width: %s -> %s" % (widthsBefore, widthsAfter))
        check(widthsBefore == list(module.TABLE_COLUMN_WIDTHS),
              "they are the fixed starting widths")

        for removed in ("ExpansionPreviewBox", "ExpansionPreviewLabel"):
            try:
                module.scriptDialog.GetValue(removed)
                check(False, "%s is gone" % removed)
            except Exception:
                check(True, "%s is gone" % removed)

        check(module.combinationTree.columnWidth(0) == module.TREE_COLUMN_WIDTH,
              "the tree column has a fixed width as well (%d)" % module.combinationTree.columnWidth(0))
        print("")
        print("selected cells are highlighted")
        SelectCells(module, [(rowA, "frames"), (rowB, "frames")])
        module.RefreshCellHighlight()
        highlighted = RowWidget(module, rowA, "frames")
        check(highlighted.palette().color(QtGui.QPalette.Base)
              == module.combinationTable.palette().color(QtGui.QPalette.Highlight),
              "a selected cell is painted in the highlight colour")
        check(highlighted.palette().color(QtGui.QPalette.Text)
              == module.combinationTable.palette().color(QtGui.QPalette.HighlightedText),
              "with the highlighted text colour")
        check(RowWidget(module, rowB, "chunk_size").palette().color(QtGui.QPalette.Base)
              != module.combinationTable.palette().color(QtGui.QPalette.Highlight),
              "and a cell that is not selected is not")

        logPath = os.path.join(work, "BlenderSubmission.log")
        check(os.path.isfile(logPath), "the click diagnostics were written: %s" % logPath)
        if os.path.isfile(logPath):
            with open(logPath, "r", errors="replace") as handle:
                lines = handle.read().splitlines()
            check(any("press row=" in line for line in lines),
                  "recording the presses: %s" % lines[-2:])
        print("")
        print("a click keeps adding to the selection, however many cells are picked")
        SelectCells(module, [])
        clicks = [(row, "frames") for row in range(module.combinationTable.rowCount())]
        clicks += [(0, "engine"), (0, "chunk_size"), (0, "resolution_x")]
        for step, (row, key) in enumerate(clicks):
            widget = RowWidget(module, row, key)
            widget.setFocus()
            modifiers = QtCore.Qt.NoModifier if step == 0 else QtCore.Qt.ControlModifier
            QtTest.QTest.mouseClick(widget, QtCore.Qt.LeftButton, modifiers)
            expected = step + 1
            check(len(module.SelectedCells()) == expected,
                  "click %d left %d cell(s) selected, expected %d: %s"
                  % (step + 1, len(module.SelectedCells()), expected, module.SelectedCells()))

        # A focus change of any editor must not destroy the selection either.
        RowWidgets(module, 0)[4].setFocus()
        check(len(module.SelectedCells()) == len(clicks),
              "moving the focus kept the selection: %s" % module.SelectedCells())

        print("")
        print("clicking the inside of an editor selects its cell too")
        # A spin box hands the mouse and the focus to an internal line edit, and a drop-down to
        # its popup: those are the parts the filter has to cover as well.
        SelectCells(module, [])
        SelectCell = module.SelectCell
        SelectCell(0, module.WIDGET_COLUMNS["frames"])
        from PyQt5 import QtWidgets

        spinInner = RowWidget(module, 0, "chunk_size").findChild(QtWidgets.QLineEdit)
        check(spinInner is not None, "the spin box has an inner line edit to click")

        if spinInner is not None:
            QtTest.QTest.mouseClick(spinInner, QtCore.Qt.LeftButton, QtCore.Qt.ControlModifier)
            selectedKeys = sorted(key for _, key in module.SelectedCells())
            check(selectedKeys == ["chunk_size", "frames"],
                  "clicking 'frames per task' selected its cell as well: %s" % module.SelectedCells())

        comboInner = RowWidget(module, 0, "engine").findChild(QtWidgets.QWidget)
        QtTest.QTest.mouseClick(RowWidget(module, 0, "engine"), QtCore.Qt.LeftButton,
                                QtCore.Qt.ControlModifier)
        selectedKeys = sorted(key for _, key in module.SelectedCells())
        check(selectedKeys == ["chunk_size", "engine", "frames"],
              "and so did the render engine drop-down: %s" % module.SelectedCells())

        popup = RowWidget(module, 0, "engine").view()
        check(popup not in module.editorWidgets,
              "the list a drop-down opens is not treated as a click on the cell")

        check(RowWidget(module, 0, "output").parent() is not None, "the path lives in a container")
        QtTest.QTest.mouseClick(RowWidget(module, 0, "output"), QtCore.Qt.LeftButton,
                                QtCore.Qt.ControlModifier)
        selectedKeys = sorted(key for _, key in module.SelectedCells())
        check(selectedKeys == ["chunk_size", "engine", "frames", "output"],
              "and the output path cell: %s" % module.SelectedCells())

        print("")
        print("shift-click selects a range from the anchor")
        SelectCells(module, [])
        SelectCell = module.SelectCell
        SelectCell(0, module.WIDGET_COLUMNS["frames"])
        SelectCell(module.combinationTable.rowCount() - 1, module.WIDGET_COLUMNS["frames"],
                   QtCore.Qt.ShiftModifier)
        selectedRows = sorted(row for row, key in module.SelectedCells() if key == "frames")
        check(selectedRows == list(range(module.combinationTable.rowCount())),
              "the whole column of frames is selected: %s" % selectedRows)

        print("")
        print("right-clicking an editor opens the menu of its cell")
        # The editors swallow the right-click, so the event filter has to produce the menu.
        # exec_() would block in a test, so the builder is recorded instead of shown.
        recorded = []
        realBuild = module.BuildCombinationTableMenuSafely

        def RecordingBuild(row=-1, column=-1, widget=None):
            recorded.append((row, column, widget is not None))
            return None

        module.BuildCombinationTableMenuSafely = RecordingBuild
        try:
            from PyQt5 import QtGui

            SelectCells(module, [])
            for widget, expectedKey in ((RowWidgets(module, rowA)[3], "engine"),
                                        (RowWidgets(module, rowA)[0], "output")):
                position = widget.rect().center()
                event = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Mouse,
                                                widget.mapToGlobal(position))
                QtCore.QCoreApplication.sendEvent(widget, event)
                check(module.SelectedCells() == [(rowA, expectedKey)],
                      "the right-click on %s selected its cell: %s"
                      % (expectedKey, module.SelectedCells()))
        finally:
            module.BuildCombinationTableMenuSafely = realBuild

        check([(row, column) for row, column, _ in recorded] == [
                  (rowA, module.WIDGET_COLUMNS["engine"]), (rowA, module.WIDGET_COLUMNS["output"])],
              "and asked for the menu of that cell: %s" % recorded)
        check([withWidget for _, _, withWidget in recorded] == [False, True],
              "with the presets only for the output column: %s" % recorded)

        print("")
        print("resetting one column of the selection")
        SelectCells(module, [(rowA, "resolution_x"), (rowB, "resolution_x")])
        module.ResetSelectedCellsToDefaults()
        check(RowWidgets(module, rowA)[6].value() == 1920 and RowWidgets(module, rowB)[6].value() == 1920,
              "the resolution of both rows is back to the scene's (%d, %d)"
              % (RowWidgets(module, rowA)[6].value(), RowWidgets(module, rowB)[6].value()))
        check(RowWidgets(module, rowA)[3].currentText() == "cycles"
              and RowWidgets(module, rowB)[1].text() == "1-42",
              "while the engine and the frame list of the selection were left alone")

        # Leave the table as it was found, for the sections below.
        module.ResetRowsToDefaults(list(range(module.combinationTable.rowCount())))
        RunMenuEntry(module, sceneItem, "Clear The Whole Tree")
        module.CombinationSelectionChanged()
        check(module.combinationTable.rowCount() == 0, "the table is empty again for the next section")

        print("")
        print("output presets")
        TickItem(sceneItem.child(0).child(0))
        module.CombinationSelectionChanged()
        check(module.combinationTable.rowCount() == 1, "one row to apply a preset to")
        presets = module.OutputPresetsFor("Scene_Main", "Beauty", "ShotCam_0")
        check(len(presets) == 3, "three presets are built (%d)" % len(presets))
        check(presets[0][0] == r"{DEFAULT_PATH}\{Scene}_{ViewLayer}\{Scene}_{ViewLayer}_{Camera}_####.png",
              "the menu shows the pattern with the placeholders: %r" % presets[0][0])
        check(presets[0][1] == os.path.join(outputDirectory, "Scene_Main_Beauty",
                                            "Scene_Main_Beauty_ShotCam_0_####.png"),
              "and the path has this row's names and the frame placeholder: %r" % presets[0][1])
        check("####" in presets[0][1], "the frame numbers are part of the preset")

        presetRow = 0
        module.ApplyOutputPreset([presetRow], 0)
        entry = module.combinationRows[presetRow]
        check(RowWidgets(module, presetRow)[0].text()
              == module.OutputPresetsFor(entry["scene"], entry["view_layer"], entry["camera"])[0][1],
              "applying it used that row's own names: %r" % RowWidgets(module, presetRow)[0].text())
        check("####" in RowWidgets(module, presetRow)[0].text(),
              "and kept the frame placeholder")

        print("")
        print("every row has a button that chooses the output path")
        browse = OutputBrowseButton(module, presetRow)
        check(browse is not None, "the browse button is in the output cell")
        check(browse is not None and "Choose" in browse.toolTip(),
              "and explains itself: %r" % (browse.toolTip() if browse else None))

        # The button has to put the chosen path into the row. The file dialog itself is stubbed:
        # opening a modal native dialog inside the test would block.
        from PyQt5 import QtWidgets

        chosenPath = os.path.join(outputDirectory, "chosen", "picked_####.exr")
        realGetSaveFileName = QtWidgets.QFileDialog.getSaveFileName
        QtWidgets.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (chosenPath, ""))
        try:
            module.BrowseForOutput(module.combinationRows[presetRow]["widgets"]["output"])
        finally:
            QtWidgets.QFileDialog.getSaveFileName = realGetSaveFileName
        check(RowWidgets(module, presetRow)[0].text() == chosenPath,
              "the button writes the chosen path into the row: %r"
              % RowWidgets(module, presetRow)[0].text())

        print("")
        print("the right-click menus survive being built and garbage collected")
        # Regression test: taking the actions of widget.createStandardContextMenu() left the
        # menu pointing at deleted actions, which crashed the dialog when it was opened.
        import gc

        for row, cell in ((presetRow, module.combinationRows[presetRow]["widgets"]["output"]),
                          (0, None)):
            menu = module.BuildCombinationTableMenuSafely(row, cell)
            check(menu is not None, "the menu of row %d was built" % row)
            gc.collect()
            if menu is not None:
                menuLabels = [action.text() for action in menu.actions()]
                check(len(menuLabels) > 0 and any("Reset" in label for label in menuLabels),
                      "its entries are alive after a collection: %s" % menuLabels)

        print("")
        print("a default without an extension is treated as a folder")
        folder, extension = module.OutputBaseAndExtension(os.path.join(outputDirectory, "####"))
        check(folder == outputDirectory and extension == "",
              "the #s are dropped: %r / %r" % (folder, extension))
        folder, extension = module.OutputBaseAndExtension(os.path.join(outputDirectory, "beauty_####.png"))
        check(folder == outputDirectory and extension == ".png",
              "with an extension the folder is its directory: %r / %r" % (folder, extension))

        print("")
        print("the right-click menu of the tree (unchanged)")
        RunMenuEntry(module, sceneItem, "Select All")
        module.CombinationSelectionChanged()
        check(len(CheckedLeafTexts(tree)) == 4, "the whole scene is ticked for this section")
        labels = MenuLabels(module, sceneItem)
        check("Select All" in labels and "Select None" in labels and "Invert Selection" in labels,
              "the selection entries are there: %s" % labels)
        check("Only This Branch" not in labels, "Only This Branch was removed: %s" % labels)

        check(RunMenuEntry(module, sceneItem.child(1), "Select None"), "Select None on a layer")
        check(CheckedLeafTexts(tree) == ["Scene_Main/Beauty/ShotCam_0", "Scene_Main/Beauty/ShotCam_1"],
              "the Mask branch was cleared: %s" % CheckedLeafTexts(tree))
        check(table.rowCount() == 2, "and the table has two rows (%d)" % table.rowCount())

        check(RunMenuEntry(module, sceneItem.child(1), "Invert Selection"), "Invert Selection on a layer")
        check(len(CheckedLeafTexts(tree)) == 4, "inverting the empty branch ticks it: %s" % CheckedLeafTexts(tree))

        check(RunMenuEntry(module, sceneItem, "Select All"), "Select All on the scene")
        check(sceneItem.checkState(0) == QtCore.Qt.Checked
              and all(sceneItem.child(index).checkState(0) == QtCore.Qt.Checked
                      for index in range(sceneItem.childCount())),
              "Select All ticks the parents as well")
        check(RunMenuEntry(module, sceneItem.parent() or sceneItem, "Select None")
              or RunMenuEntry(module, sceneItem, "Clear The Whole Tree"), "Clear The Whole Tree")
        check(CheckedLeafTexts(tree) == [], "everything is unticked")
        check(sceneItem.checkState(0) == QtCore.Qt.Unchecked
              and all(sceneItem.child(index).checkState(0) == QtCore.Qt.Unchecked
                      for index in range(sceneItem.childCount())),
              "and the parents follow back to unchecked")
        check(table.rowCount() == 0, "and the table is empty (%d)" % table.rowCount())

        print("")
        print("Copy First Row To All")
        TickItem(sceneItem.child(0))
        check(table.rowCount() == 2, "two rows again (%d)" % table.rowCount())
        RowWidgets(module, 0)[3].setCurrentText("eevee")
        RowWidgets(module, 0)[4].setCurrentText("PNG")
        RowWidgets(module, 0)[8].setChecked(True)     # strict error checking
        secondOutputBefore = RowWidgets(module, 1)[0].text()
        module.CopyFirstRowToAll()
        second = RowWidgets(module, 1)
        check(second[3].currentText() == "eevee" and second[4].currentText() == "PNG"
              and second[8].isChecked(), "the second row got the first row's render options")
        check(second[0].text() == secondOutputBefore,
              "but not its output file, which would make the jobs overwrite each other")

        print("")
        print("the confirmation dialog")
        details = module.SubmissionDetails(module.BuildJobs(
            dict(module.CollectValues(), combinations=[
                dict(module.CombinationRows()[0], name="shot_010 [Beauty / ShotCam_0]"),
                dict(module.CombinationRows()[0], name="shot_010 [Beauty / ShotCam_1]")]))[0],
            module.CollectValues())
        for label in ("Combination", "Output File", "Frame List", "Frames Per Task", "Render Engine",
                      "Image Format", "Cycles GPU", "Res X", "Res Y", "Strict Error",
                      "Override Markers"):
            check(("%s" % label) in details or ("%-17s:" % label) in details,
                  "the confirmation lists %s" % label)

        # 'Use Scene Setting' is resolved to what the .blend file has.
        check("Use Scene Setting" not in details,
              "no setting is shown as 'Use Scene Setting':\n%s" % details)
        # The value comes from the scene of the job, not from the file-wide context.
        mainDetails = module.submitContext["scene_details"]["Scene_Main"]
        altDetails = module.submitContext["scene_details"]["Scene_Alt"]
        expectedEngine = module.EngineDisplayName(mainDetails["engine"])
        check(("Render Engine    : %s" % expectedEngine) in details,
              "the render engine shows the one the scene has (%r)" % expectedEngine)
        check(("Image Format     : %s" % mainDetails["image_format"]) in details,
              "the image format shows the one the scene has")
        check(("Res X            : %d" % mainDetails["resolution_x"]) in details,
              "and the resolution the scene has")

        # A row of the other scene with its settings left alone: engine and format at "Use Scene
        # Setting", resolution at 0, so every value has to be resolved from that scene.
        altCombination = dict(module.DefaultCombinationSettings("Scene_Alt"),
                              scene="Scene_Alt", name="alt", view_layer="Layers_A", camera="AltCam",
                              output_file="E:\\tmp\\alt_####.png", frames="1-10", chunk_size=1,
                              strict_error=False, marker_override=False,
                              resolution_x=0, resolution_y=0)
        other = module.SubmissionDetails([altCombination], module.CollectValues())
        check(module.EngineDisplayName(altDetails["engine"]) in other,
              "a job of the other scene resolves its own scene's engine: %r"
              % module.EngineDisplayName(altDetails["engine"]))
        check(("Image Format     : %s" % altDetails["image_format"]) in other,
              "and its own image format")
        check(("Res X            : %d" % altDetails["resolution_x"]) in other,
              "and its own resolution")
        check(module.ConfirmationHeadline([1, 2]) == "Submits 2 jobs",
              "the headline only counts: %r" % module.ConfirmationHeadline([1, 2]))
        check(module.ConfirmationHeadline([1]) == "Submits 1 job",
              "and it is singular for one job: %r" % module.ConfirmationHeadline([1]))

        jobsForDialog = module.BuildJobs(dict(module.CollectValues(), combinations=[
            module.CombinationRows()[0], module.CombinationRows()[1]]))[0]
        dialog = module.BuildConfirmationDialog(jobsForDialog, module.CollectValues())
        check(dialog is not None, "the confirmation dialog is built")
        from PyQt5 import QtWidgets as QtWidgetsModule

        textBox = dialog.findChild(QtWidgetsModule.QPlainTextEdit)
        check(textBox is not None and textBox.isReadOnly(),
              "with a read-only text box")
        check(textBox is not None
              and textBox.lineWrapMode() == QtWidgetsModule.QPlainTextEdit.NoWrap,
              "that does not wrap, so it scrolls sideways")
        check(textBox is not None
              and textBox.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAsNeeded
              and textBox.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarAsNeeded,
              "and scrolls in both directions")
        check(textBox is not None and "Output File" in textBox.toPlainText(),
              "listing the options of every job")
        labels = [child.text() for child in dialog.findChildren(QtWidgetsModule.QLabel)]
        check(labels == ["Submits 2 jobs"], "and only the job count above it: %s" % labels)
        dialog.close()

        print("")
        print("Submit writes one job per row with that row's options")
        module.scriptDialog.SetValue("NameBox", "shot_010")
        module.SubmitButtonPressed()
        check(module.testConfirmations == [2],
              "the submission asked for confirmation once: %s" % module.testConfirmations)

        check(len(commands) == 1, "exactly one submission call (%d)" % len(commands))
        if commands:
            arguments = commands[0]
            check(arguments[0] == "-SubmitMultipleJobs",
                  "the submission uses -SubmitMultipleJobs (%r)" % (arguments[0],))
            check(arguments.count("-Job") == 2, "one -Job group per row (%d)" % arguments.count("-Job"))

            folder = os.path.join(work, "blender_expand_%d" % os.getpid())
            files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
            check(files == ["blender_job_info_001.job", "blender_job_info_002.job",
                            "blender_plugin_info_001.job", "blender_plugin_info_002.job"],
                  "the four files are there: %s" % files)

            if files:
                with open(os.path.join(folder, "blender_job_info_001.job"), "r", encoding="utf-16") as handle:
                    firstJob = handle.read()
                with open(os.path.join(folder, "blender_plugin_info_001.job"), "r", encoding="utf-16") as handle:
                    firstPlugin = handle.read()
                with open(os.path.join(folder, "blender_job_info_002.job"), "r", encoding="utf-16") as handle:
                    secondJob = handle.read()
                with open(os.path.join(folder, "blender_plugin_info_002.job"), "r", encoding="utf-16") as handle:
                    secondPlugin = handle.read()

                check("Frames=1-10" in firstJob and "ChunkSize=1" in firstJob,
                      "the job carries the frame list and frames per task of its row")

                def OutputOf(text):
                    match = re.search(r"OutputFilename0=(.+)", text)
                    return match.group(1).strip() if match else ""

                firstOutput = OutputOf(firstJob)
                secondOutput = OutputOf(secondJob)
                check(firstOutput != "" and secondOutput != "" and firstOutput != secondOutput,
                      "the two rows write to different files: %r / %r" % (firstOutput, secondOutput))
                rowPath = RowWidgets(module, 0)[0].text()
                check(firstOutput == rowPath and "####" in firstOutput,
                      "a row with its own path keeps it verbatim: %r" % firstOutput)
                check("ShotCam_1" in secondOutput and "beauty_####.png" not in secondOutput,
                      "the row with the default path gets the name suffix: %r" % secondOutput)

                for name, text in (("first", firstPlugin), ("second", secondPlugin)):
                    check("RenderEngine=eevee" in text and "ImageFormat=PNG" in text
                          and "StrictErrorChecking=True" in text,
                          "the %s job carries its row's options" % name)
                    check("ViewLayer=Beauty" in text and "RenderScene=Scene_Main" in text,
                          "and the view layer and scene it was ticked for (%s)" % name)
                check("Camera=ShotCam_0" in firstPlugin, "the first job is the first camera")
                check("Camera=ShotCam_1" in secondPlugin, "the second job is the other camera")

        print("")
        print("nothing ticked is refused")
        del commands[:]
        del messages[:]
        RunMenuEntry(module, sceneItem, "Clear The Whole Tree")
        module.SubmitButtonPressed()
        check(len(commands) == 0, "nothing was submitted")
        check(any("combination tree" in message for title, message in messages),
              "the reason is explained: %s" % [title for title, _ in messages])

        print("")
        print("a half-specified resolution is refused")
        del commands[:]
        del messages[:]
        TickItem(sceneItem.child(0))
        RowWidgets(module, 0)[6].setValue(1920)      # Res X set,
        RowWidgets(module, 0)[7].setValue(0)         # Res Y not
        module.SubmitButtonPressed()
        check(len(commands) == 0, "nothing was submitted")
        check(any("Res X and Res Y" in message for title, message in messages),
              "the reason is explained: %s" % [title for title, _ in messages])
        RowWidgets(module, 0)[6].setValue(0)

        print("")
        print("an invalid frame list or two rows with the same output file are refused")
        del commands[:]
        del messages[:]
        RowWidgets(module, 0)[1].setText("not a range")
        module.SubmitButtonPressed()
        check(len(commands) == 0 and any("frame list" in message for title, message in messages),
              "the frame list is validated per row: %s" % [title for title, _ in messages])

        del commands[:]
        del messages[:]
        shared = os.path.join(outputDirectory, "same_for_every_row_####.png")
        RowWidgets(module, 0)[1].setText("1-10")
        RowWidgets(module, 0)[0].setText(shared)
        TickItem(sceneItem.child(0).child(1))        # a second row
        RowWidgets(module, RowForLabel(module, "Beauty / ShotCam_1"))[0].setText(shared)
        if module.combinationTable.rowCount() == 2:
            module.SubmitButtonPressed()
            check(len(commands) == 0, "nothing was submitted")
            check(any("same output file" in message for title, message in messages),
                  "and the duplicate output path is explained: %s" % [title for title, _ in messages])

        print("")
        print("combinations from two scenes can be submitted together")
        del commands[:]
        del messages[:]
        RunMenuEntry(module, sceneItem, "Clear The Whole Tree")
        TickItem(otherSceneItem.child(0).child(0))          # Scene_Alt / Layers_A / AltCam
        check(CheckedLeafTexts(tree) == ["Scene_Alt/Layers_A/AltCam"],
              "a combination of the other scene can be ticked: %s" % CheckedLeafTexts(tree))
        check(table.rowCount() == 1, "and it gets a row of its own (%d)" % table.rowCount())
        engine = RowWidgets(module, 0)[3]
        engine.setCurrentText("workbench")                  # only this scene's job should get it

        TickItem(sceneItem.child(0).child(1))               # Scene_Main / Beauty / ShotCam_1
        module.CombinationSelectionChanged()
        check(table.rowCount() == 2, "two rows now (%d)" % table.rowCount())
        check(RowForLabel(module, "Layers_A / AltCam") >= 0,
              "the other scene's row is named after its own view layer: %s"
              % [table.item(row, 0).text() for row in range(table.rowCount())])

        module.SubmitButtonPressed()
        check(len(commands) == 1, "one submission call (%d)" % len(commands))
        if commands:
            check(commands[0].count("-Job") == 2, "one -Job group per scene combination")
            folder = os.path.join(work, "blender_expand_%d" % os.getpid())
            jobs = {}
            for index in (1, 2):
                with open(os.path.join(folder, "blender_job_info_%03d.job" % index), "r",
                          encoding="utf-16") as handle:
                    jobs[index] = handle.read()
                with open(os.path.join(folder, "blender_plugin_info_%03d.job" % index), "r",
                          encoding="utf-16") as handle:
                    jobs[str(index)] = handle.read()

            combined = " ".join(jobs.values())
            check("RenderScene=Scene_Main" in combined and "RenderScene=Scene_Alt" in combined,
                  "each job names its own scene")
            check("Layers_A" in combined and "AltCam" in combined,
                  "and the other scene's view layer and camera")
            check("Scene_Main" in combined and "Scene_Alt" in combined,
                  "the scene appears in the job names and output paths too")

        print("")
        print("one combination keeps the plain name and the untouched output")
        del commands[:]
        del messages[:]
        RowWidgets(module, 0)[6].setValue(0)
        RowWidgets(module, 0)[7].setValue(0)
        RunMenuEntry(module, sceneItem, "Clear The Whole Tree")
        cameraItem = sceneItem.child(0).child(1)
        TickItem(cameraItem)
        module.SubmitButtonPressed()

        check(not [message for message in messages if message[0] == "Error"],
              "no error dialog (%s)" % [title for title, _ in messages])
        check(len(commands) == 1, "one submission call (%d)" % len(commands))
        if commands:
            arguments = commands[0]
            check([os.path.basename(argument) for argument in arguments]
                  == ["blender_job_info.job", "blender_plugin_info.job"],
                  "the historical file names: %s" % [os.path.basename(argument) for argument in arguments])

            with open(arguments[0], "r", encoding="utf-16") as handle:
                jobText = handle.read()
            check("Name=shot_010\n" in jobText, "the plain job name is written")
            check("BatchName" not in jobText, "a single job is not grouped")

        print("")
        print("a context without the per-scene map is reported instead of silently showing one scene")
        module2, commands2, messages2, shown2 = LoadSubmissionModule(work)
        oldContext = dict(context)
        del oldContext["scene_details"]
        module2.__main__(
            sceneFile,
            "1-10",
            os.path.join(outputDirectory, "beauty_####.png"),
            "0",
            "64bit",
            base64.b64encode(json.dumps(oldContext).encode("utf-8")).decode("ascii"),
        )
        check(module2.combinationTree.topLevelItemCount() == 1,
              "an older proxy can only be listed as the active scene (%d)"
              % module2.combinationTree.topLevelItemCount())
        hint = str(module2.scriptDialog.GetValue("TreeHintBox"))
        check("did not send the other scenes" in hint,
              "and the dialog says why instead of pretending that is all there is: %r" % hint)

        print("")
        if FAILURES:
            print("RESULT: %d check(s) failed" % len(FAILURES))
            for failure in FAILURES:
                print("  - %s" % failure)
            return 1
        print("RESULT: the tree and the options table drive the submission")
        return 0
    except Exception:
        print("RESULT: FAILED - exception in the test itself:")
        print(traceback.format_exc())
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(__main__())
