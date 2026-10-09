"""What the Deadline dialog API itself offers - control types and their signatures.

    deadlinecommand -ExecuteScript custom\\tests\\report_dialog_api.py
"""

import inspect
import os
import sys


def __main__():
    print("python %s" % sys.version.split()[0])

    from DeadlineUI.Controls.Scripting.DeadlineScriptDialog import DeadlineScriptDialog

    print("")
    print("Add*ToGrid methods of DeadlineScriptDialog")
    for name in sorted(dir(DeadlineScriptDialog)):
        if not name.startswith("Add"):
            continue
        try:
            signature = str(inspect.signature(getattr(DeadlineScriptDialog, name)))
        except Exception as error:
            signature = "<%s>" % error
        print("  %-34s %s" % (name, signature))

    print("")
    print("the control types the *options* files can use (Job Properties)")
    try:
        import DeadlineUI.Controls.DeadlineUIControls as controls
        names = [name for name in dir(controls) if name.endswith("Control")]
        print("  " + ", ".join(sorted(names)))
    except Exception as error:
        print("  <could not import: %s>" % error)

    print("")
    print("a text control: is it always editable?")
    import inspect as _inspect
    source = ""
    try:
        source = _inspect.getsource(DeadlineScriptDialog.AddControlToGrid)
    except Exception as error:
        source = "<no source: %s>" % error
    for line in source.splitlines()[:24]:
        print("  " + line)

    print("")
    print("is PyQt5 available inside a script dialog?")
    try:
        from PyQt5 import QtCore, QtWidgets
        print("  yes: Qt %s, QApplication.instance()=%r"
              % (QtCore.QT_VERSION_STR, QtWidgets.QApplication.instance()))
    except Exception as error:
        print("  no: %s" % error)

    return 0


if __name__ == "__main__":
    sys.exit(__main__())
