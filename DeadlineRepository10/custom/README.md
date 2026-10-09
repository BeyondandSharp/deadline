# Deadline Blender integration — custom overrides

This folder adds the render controls that the **AWS Deadline Cloud** Blender submitter
offers to the **self-hosted Deadline 10** repository, without touching a single
factory file.

Everything here lives under the repository's `custom` folder, which Deadline loads in
preference to the factory folders and which the repository installer leaves alone
(and backs up) during upgrades.

```
custom/
  plugins/Blender/Blender.py                  Worker application plugin (override)
  plugins/Blender/Blender.param               Plugin configuration (override)
  plugins/Blender/Blender.options             Blender Settings page of Job Properties (override)
  plugins/Blender/BlenderRenderScript.py      Applies the settings and renders, inside Blender
  scripts/Submission/BlenderSubmission.py     Submission dialog (override)
  submission/Blender/Main/SubmitBlenderToDeadline.py  Blender-side proxy (override)
  scripts/Jobs/blender_render_options.py      Monitor job script: drop-downs for Scene / layer / camera
  lib/blend_names.py                          Reads those names out of a .blend without loading it
  lib/blender_asset_tracer/                   Vendored parser (BAT 1.20, GPL-2.0-or-later)
  lib/7zip/                                   Bundled 7-Zip-zstd, unpacks compressed .blend files
  tests/verify_custom_overrides.py           Checks that Deadline resolves everything from custom/
  tests/report_paths.py                      Shows which copy of each file a machine actually uses
  tests/test_render_script.py                Runs the render script against a local Blender
  tests/test_blend_reader.py                 Compares the .blend reader against Blender itself
  tests/test_job_script.py                   Key-drift and validation checks for the job script
  tests/test_job_script_dialog.py            Builds the job dialog with the real Deadline dialog API
  tests/test_submission_expansion.py         Combination logic, job files and submit arguments
  tests/test_submission_dialog.py            Builds the submission dialog and submits through it
  tests/test_submit_context.py               Checks the scene / layer / camera data Blender sends
  tests/report_dialog_api.py                 Prints what the Deadline dialog API offers (not a test)
  README.md                                   This file
```

## Why this works

Per the Deadline 10 documentation:

* [`custom` folder](https://docs.thinkboxsoftware.com/products/deadline/10.4/1_User%20Manual/manual/scripting-overview.html)
  — "any scripts or plugins in the 'custom' folder will override any scripts or plugins
  that are shipped with Deadline if they share the same name", and they are not affected
  by repository upgrades. It lists `custom/plugins/`, `custom/events/`,
  `custom/scripts/Submission/` and friends as the supported locations.
* [`GetRepositoryPath` / `GetRepositoryFilePath`](https://docs.thinkboxsoftware.com/products/deadline/10.4/1_User%20Manual/manual/command-line-arguments-repo.html)
  — the `<Custom>` argument defaults to `True`, i.e. the custom path is returned first.
  The stock Blender code already relies on this default:
  `DeadlineBlenderClient.py` calls `-GetRepositoryPath submission/Blender/Main`, and the
  proxy calls `GetRepositoryFilePath("scripts/Submission/BlenderSubmission.py")`.
  Both therefore resolve to this folder automatically — **no artist-side reinstall of
  the Blender add-on is needed**.
* [Application Plugins](https://docs.thinkboxsoftware.com/products/deadline/10.4/1_User%20Manual/manual/application-plugins.html)
  — `custom/plugins/` is the documented place for application plugins, and the Worker
  log prints the path a plugin was loaded from, which is the evidence to look for.

## What was ported from Deadline Cloud

Every option defaults to *"Use Scene Setting"*, i.e. "render what the `.blend` file
says". A job that does not use the new controls behaves exactly like it did with the
stock Blender plugin.

| Deadline Cloud capability | Where it lives here | Default |
| --- | --- | --- |
| Render engine selection (Cycles / EEVEE / Workbench) | dialog `Render Engine` → `BlenderRenderScript.py` | use scene setting |
| Scene selection | dialog `Scene` | use scene setting |
| View layer selection | dialog `View Layer` (other layers are disabled for the render) | use scene setting |
| Camera selection | dialog `Camera` | use scene setting |
| Camera timeline-marker override | dialog `Override Camera Markers` | off |
| Cycles GPU device + fallback | dialog `Cycles GPU` (OptiX → CUDA → HIP → oneAPI → Metal → CPU) | use scene setting |
| Output image format override | dialog `Image Format` | use scene setting |
| Resolution override | dialog `Resolution X / Y` | 0 = use scene setting |
| Strict error checking | dialog `Strict Error Checking` + plugin config `Blender_StrictErrorRegex` | off |
| Per-frame task granularity | not ported — use the existing `Frames Per Task` (chunking), which is strictly more capable | — |
| Sticky/daemon rendering, S3 job attachments, queues/fleets, telemetry, update checks | deliberately not ported (cloud-only or poor cost/benefit) | — |

Additionally this deployment folds in **multi-version Blender executable selection**
(previously maintained as hand-copied patches in the repository) — see
[Render executable selection](#render-executable-selection) below.

Two long-standing defects of the stock plugin are fixed along the way:

* `[SuppressOutput]` never had any effect (the plugin read a misspelled job key,
  `SupressOutput`). The job key is still honoured when present; the plugin configuration
  entry now works.
* `HandleStdoutError` was dead code (its registration was commented out) and EEVEE had
  no progress reporting. EEVEE sample progress is now reported, and error matching is
  available as an opt-in.

## Deployment

1. Copy this `custom` folder into the repository root, e.g.
   `\\repo-server\DeadlineRepository10\custom\`.
2. Optional, to keep the Blender icon in the Monitor: copy
   `plugins\Blender\Blender.ico` into `custom\plugins\Blender\` (binary file, so it is
   not shipped here).
3. Run the verification script (see below). It decides whether everything below
   resolves into `custom\`.
4. Open the Monitor in Power User mode → **Tools → Configure Plugins → Blender** and
   check the `Blender Executable` template: the shipped value covers the conventional
   install locations through the `{version}` / `{version_full}` placeholders, so it only
   needs editing for a non-standard layout. Any value already stored in the database
   takes precedence over the shipped default — clear the field if you want the new
   template to apply.
5. Make sure the Blender plugin is **Enabled** and that new plugins are allowed to be
   auto-enabled (`NewApplicationPluginsEnabled` in the Repository Options).

Nothing needs to be restarted for the submission side. For the Worker side, the plugin
is loaded per job; a Worker that already has the plugin loaded picks up the change on
its next job (or immediately if "Reload Plugin Between Tasks" is enabled).

## Verification

```
deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\verify_custom_overrides.py"
```

Expected: every entry prints `PASS` and the summary says `PASSED`. If any entry fails,
Deadline is not resolving that file through `custom/`; the fallbacks are printed by the
script and listed in `tests/verify_custom_overrides.py`.

Two extra spot checks:

```
deadlinecommand -GetRepositoryPath "submission/Blender/Main"
deadlinecommand -GetRepositoryFilePath "plugins/Blender/BlenderRenderScript.py"
```

Both should return a path containing `custom`. Also confirm in a Worker task log that the
plugin was loaded from `custom\plugins\Blender`.

## Automated tests

Most of them run **without Deadline**, which makes them the fastest way to check a change
(two of them are what caught the option-name/transport bug and the view-layer ordering bug
during development):

```
<repo>\.venv\Scripts\python.exe "<DeadlineRepository>\custom\tests\test_render_script.py"     [blender.exe]
<repo>\.venv\Scripts\python.exe "<DeadlineRepository>\custom\tests\test_blend_reader.py"      [blender.exe]
<repo>\.venv\Scripts\python.exe "<DeadlineRepository>\custom\tests\test_job_script.py"
<repo>\.venv\Scripts\python.exe "<DeadlineRepository>\custom\tests\test_submission_expansion.py"
<repo>\.venv\Scripts\python.exe "<DeadlineRepository>\custom\tests\test_submit_context.py"    [blender.exe]
```

Two of them need Deadline itself, because they use the real dialog API:

```
deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\test_job_script_dialog.py"
deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\test_submission_dialog.py"
```

`test_render_script.py` runs the render script with the exact command line shape the plugin
builds, against a locally installed Blender (without an argument it tests every Blender it
finds under `C:\Program Files\Blender Foundation` and on `PATH`). It checks:

* engine override + resolution + frame range + custom output path really take effect
  (it saves the test scene with Cycles, asks for Workbench, and requires the render log to
  report the Workbench engine id);
* an output directory containing spaces;
* the EEVEE engine id for that Blender version
  (`BLENDER_EEVEE_NEXT` for 4.2–4.x, `BLENDER_EEVEE` from 5.0);
* that a task without options fails with an explicit message;
* that an unknown scene name fails the task with the scene name in the message.

`test_blend_reader.py` generates an uncompressed and a ZStandard-compressed `.blend`,
reads the names back through `custom/lib/blend_names.py` and compares them with what Blender
itself reports, for all three read paths (parser, parser after decompressing, Blender
fallback), plus the error messages.

`test_job_script.py` loads the Monitor job script with stubbed Deadline modules and checks
that **every plugin-info key the dialog writes is declared in `Blender.options` and read by
`Blender.py`**, that its drop-down values match the option file exactly, that the render
script understands each option name, and that the validation rejects unknown scenes, view
layers of another scene and half a resolution. This is the test that guards against the
"setting had no effect" class of bug.

`test_job_script_dialog.py` needs Deadline itself (it builds the real dialog), so run it
with `deadlinecommand` rather than Python:

```
deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\test_job_script_dialog.py"
```

It builds the "Blender Render Options" dialog for real - with stubbed selected jobs and a
stubbed `ShowDialog` so no window is opened - **presses Save** and checks that all ten
plugin-info keys land in the job with the values from the controls. It also covers **every
entry path**, including the ones that used to end in silence: no job selected, a non-Blender
job, `GetSelectedJobs()` raising (Launcher or `deadlinecommand` context), a job whose
properties raise, and the dialog construction itself raising. Its fake job only offers the
members the real `Job` class has, so a wrong attribute name (like the `JobPluginName` that
broke it once) fails the test instead of reaching the farm.

`test_job_script.py` additionally checks statically that **every control name the script
reads or writes is one the dialog actually creates** - the check that would have caught
`SavePressed` asking for `SceneNameBox` while the dialog builds `SceneBox` (the submission
dialog's naming), which made Save fail with a `KeyError` on the farm.

`test_submission_expansion.py` loads the submission dialog with stubbed Deadline modules and
covers the job building: name sanitizing and collision handling, the combinations, the job names
and output paths, the job limit, **each combination's own options ending up in that job's plugin
info file**, and the files and command line a submission produces - with one job, with several,
with `-Dependent`, and with the scene submitted alongside. It also checks that the removed
controls (`EngineBox`, `SceneNameBox`, `UseTreeBox`, ...) are really gone from the script.

`test_submission_dialog.py` does the same through the **real** dialog, the **real PyQt5 tree**
and the **real options table**: it builds the dialog with a base64 context like the Blender
proxy sends, checks that the tree lists every scene of the file and opens with the active view
layer + camera ticked (with the parents partially checked), that the table has one row per
ticked combination with the scene's resolution, the submitted output path and frame list, that a
row keeps its settings while its combination stays ticked, that the two right-click menus offer
the reset entries and the output presets (and that a preset is built from that row's own names, that cells can be selected individually and
that editing one of several selected cells of a column changes exactly those cells,
and that an old proxy without the per-scene map is reported instead of silently showing one
scene. Then it presses **Submit** and inspects the files and the `deadlinecommand` arguments
that would have gone to the farm (the call itself is recorded, not executed), including the
per-row output file, `Frames` and `ChunkSize`. It also checks the refusals (nothing ticked, half
a resolution, an invalid frame list, two rows with the same output file) and that one ticked
combination keeps the historical files and command.

`test_submit_context.py` builds a two-scene `.blend` in a real Blender, then asks the Blender
proxy for the submission context and checks what it carries: **every scene with its own view
layers and its own cameras** (a scene that is not on screen included), plus the active scene,
view layer and camera that the tree ticks by default. This is the data the render tree is
filled from, and exactly what an earlier version could not provide.

`report_paths.py` is not a test but a deployment probe - see "I changed a file but nothing
changed" above.

### Acceptance tests on the farm

| # | Test | Expected |
| --- | --- | --- |
| 1 | Submit with no new option selected | Identical to the pre-migration behaviour: same frames, chunking, output names, progress and `Saved:` counting |
| 2 | Engine: Cycles / EEVEE / Workbench | Each renders with the selected engine regardless of what is saved in the `.blend`. The task log line `[deadline-blender-custom] Rendering: engine=...` must name the expected engine id |
| 3 | Multi-scene file, pick a non-active scene | That scene is rendered |
| 4 | View layer selection | Only the selected layer is written out |
| 5 | Camera selection | Selected camera is used; markers unchanged unless the override is enabled |
| 6 | Cycles GPU (CUDA or OptiX) | Task log shows `gpu=CUDA`/`gpu=OPTIX` and Blender renders on the GPU |
| 7 | Image format PNG→OPEN_EXR | Output file has the expected format/extension |
| 8 | Resolution override | Output dimensions match |
| 9 | Strict error checking on a scene that logs an `Error:` | Task **fails** with the matching log line |
| 10 | Two Blender versions installed in conventional locations (e.g. 4.2 and 4.5) | Each job log shows the executable resolved through the `{version}` template for its own version; no per-version configuration exists |
| 11 | Delete `custom/plugins/Blender/BlenderRenderScript.py` | Task fails with "Could not find BlenderRenderScript.py" (never a silent success) |
| 12 | Submit from the Monitor (no version in the job) | The unversioned template paths are used, and the log reports the skipped `{version}` entries instead of failing |
| 13 | On a queued job: Job Properties → `Blender Settings` → set `Render Engine` to `eevee` (or a camera) and save | The remaining tasks render with that engine; the task log shows the `Blender render options: ...` line for the new value. Set it back to `Use Scene Setting` and the tasks go back to the `.blend` value |
| 14 | Set `Frames Per Task` = 5 in a row (a multi-frame chunk) | All five frames are written and the task progress advances per frame (one `Saved:` line each), exactly as with the stock plugin |
| 15 | Right-click the scene in the tree and `Select All`, submit | One job per view layer x camera appears under one `BatchName`, each with its own `RenderScene` / `ViewLayer` / `Camera` and its own output path; all of them render |
| 16 | Tick one view layer's box only | Exactly that layer's cameras are submitted; the other layers are untouched |
| 17 | Give two rows different engines / formats / resolutions, submit | Each job's plugin info carries only its own values (Job Properties shows them per job) and each renders that way |
| 18 | Tick nothing in the tree and submit | Refused with an explanation, nothing is submitted |
| 19 | Tick **Render One At A Time** with two combinations | The second job stays "Queued" until the first one finishes |
| 20 | Tick exactly one combination (the dialog's default) | Plain job name, untouched output path, `blender_job_info.job` / `blender_plugin_info.job`, two-argument submission command |
| 21 | Untick a combination, tick it again | Its row comes back with everything it had (output path, frames, engine, resolution …) |
| 22 | Open the dialog again after a submission | The tree starts with the current view layer + camera ticked and every row back at the defaults |
| 23 | Open a two-scene file and tick a combination of the **second** scene | Its row shows that scene's resolution, and its job renders that scene with its own view layer and camera |
| 24 | Right-click a row and `Reset Selected Rows To Defaults` after changing engine, resolution and output | The row goes back to `Use Scene Setting`, the scene's resolution and the default output path |
| 24b | Select one cell and `Reset Selected Cells To Defaults` after changing that column in several rows | Only that column of the selected cells is reset; the other columns keep their values |
| 24c | Select the frame list cell of three rows (Ctrl-click), type `1-50` in one of them and press Enter | All three rows render `1-50`; the engines, resolutions and paths of those rows are unchanged |
| 24e | Ctrl-click six or seven cells in different columns and rows, including `Frames Per Task` and `Render Engine` | Every one of them stays selected - also the ones whose editor keeps its text in a child widget - and no click clears the ones before it |
| 24f | Click one cell and Shift-click another | The rectangular range between them is selected |
| 24g | Click a frame list cell once and type | The cell is selected and takes the typing straight away; Enter writes the value into that job |
| 24i | Ctrl-click three frame list cells, then click one of them and type `1-50`, Enter | All three rows get `1-50`; the click keeps them highlighted until the selection is changed |
| 24h | Ctrl-click cells in three rows | All three are highlighted, although the cells hold real widgets (the highlight is painted into the widget, not behind it) |
| 24j | Click the blank strip on the left of a `Render Engine` cell | The cell is selected and the list does **not** open; clicking the value itself opens it |
| 24k | `Ctrl`-click the blank strips of two `Render Engine` cells | Both cells end up selected (the press does not reach the table, where Ctrl would remove the cell again) |
| 24l | Tick combinations of a second scene, or one with a very long view layer name | The window does not change size: the tree, the table columns and the `Will submit` line stay as wide as they were |
| 24d | Right-click a cell that is not part of the current selection | The selection moves to that cell before the menu opens, so the reset entries act on it |
| 25 | Right-click the `Output File` cell and pick the first preset for two rows | Each row gets its own folder and file name (no `Scene_Main_Beauty` repeated for the other camera), and the render writes into the created folder |
| 25b | Right-click a path, choose a preset, or use the `...` button | The menu opens without crashing, the preset shows the `{DEFAULT_PATH}\{Scene}_...` pattern, and the chosen/resolved path lands in the row |
| 26 | Give two rows the same explicit output path and submit | Refused with both paths named; nothing is submitted |
| 27 | Set a row's frame list to `not a range` and submit | Refused with the row named; nothing is submitted |
| 28 | Give one row `1-100` and another `1-10` and submit | `Frames=` in the two job files is 1-100 and 1-10 (each job renders only its own frames) |

## Rollback

Delete the contents of `custom/` (keep the empty folder). Nothing else was modified, so
the farm is immediately back to stock behaviour — including jobs that are still queued,
because the plugin info keys of old jobs are simply ignored.

## Upgrading the repository

`custom/` survives repository upgrades; the installer also backs it up under
`..\backup\<timestamp>\custom`. After an upgrade, re-run the verification script: if a
future Deadline release changes path resolution, the script reports it.

## How the render is triggered

The plugin starts Blender like this:

```
blender -b "<scene file>" -t <threads> --python-exit-code 1 --python "<BlenderRenderScript.py>" -- <options>
```

where `<options>` are `name=value` tokens, quoted when they contain spaces:

```
engine=workbench resx=64 resy=64 frames=1-2 threads=2 output="D:\renders\out_####" render=1
```

Blender hands everything after `--` to the script in `sys.argv`. The script applies those
settings (engine, scene, view layer, camera, format, resolution, GPU device, threads, output
path, frame range) and then renders with
`bpy.ops.render.render(animation=True, scene=...)`.

The `DLB_*` environment variables the plugin also sets are only a **fallback**: on this farm
a task ran with every one of them missing, so the command line is the transport the script
actually relies on. If the script receives **no** options at all it **fails the task** with
an explicit message instead of exiting successfully without rendering — that is the
signature of a Worker running an older copy of `Blender.py`. The task log always carries the
two lines needed to diagnose it:

```
Blender command line: blender -b "..." -t 0 --python-exit-code 1 --python "..." -- render=1 ...
[deadline-blender-custom] Blender 5.2.2 LTS, options received: {'render': '1', ...}
```

**Why not `-s/-e/-a`?** Blender does *not* execute its command line strictly from left to
right. Arguments are handled in **passes** (`ARG_PASS_ENVIRONMENT`, `ARG_PASS_SETTINGS`,
`ARG_PASS_SETTINGS_GUI`, `ARG_PASS_SETTINGS_FORCE`, `ARG_PASS_FINAL` — "Keep in order of
execution" in `creator_intern.h`), and the render flags are handled in an earlier pass
than `--python`. A script that sits before `-a` on the command line is therefore executed
*after* the render has already finished:

* the flags the parser handles (`-o`, `-x`, `-s`, `-e`, `-t`) take effect, so output paths
  and frame ranges look correct;
* anything the script changes (engine, GPU device, resolution, view layer, camera) is
  applied to a render that already happened, so nothing changes in the output.

Rendering from inside the script removes the dependency on that ordering, and matches what
the AWS Deadline Cloud adaptor does for the same reason. Deadline's semantics are
preserved: one render per task for the task's frame range, one `Saved: ...` line per frame
for progress reporting, and a non-zero exit code if anything in the script fails.

This is also why `Render engine` / `GpuDevice` / etc. changed in the dialog or in Job
Properties take effect on the tasks that have not started yet — each task re-runs the
script with the job's current plugin info.

## Render executable selection

The submitter writes the submitting Blender's version into the job (`Version` = `4.2`,
`VersionFull` = `4.2.1`). On the Worker the plugin resolves the executable like this:

1. `[Blender_<major.minor>_RenderExecutable]` — used when an administrator has added such
   a section (pinning one version to a specific installation);
2. `[Blender_RenderExecutable]` — the shared **template**, with the version substituted
   into its placeholders;
3. the stock Deadline lookup (`GetRenderExecutable`), as a last resort.

### Version placeholders

The template paths may contain two placeholders, so a single entry covers every Blender
version and no configuration change is needed when a new version is installed:

| Placeholder | Substituted with | Example |
| --- | --- | --- |
| `{version}` | `major.minor` of the submitting Blender | `4.2` |
| `{version_full}` | full version of the submitting Blender | `4.2.1` |

Shipped default (one entry per line in the Monitor, semicolons here):

```
C:\Program Files\Blender Foundation\Blender {version}\blender.exe
C:\Program Files\Blender Foundation\Blender\blender.exe
C:\Program Files (x86)\Blender Foundation\Blender {version}\blender.exe
C:\Program Files (x86)\Blender Foundation\Blender\blender.exe
/Applications/Blender {version}/blender.app/Contents/MacOS/blender
/Applications/Blender/blender.app/Contents/MacOS/blender
/usr/local/Blender {version}/blender
/usr/local/blender-{version_full}-linux-x64/blender
/usr/local/Blender/blender
```

Entries that need a placeholder are **skipped** for jobs that carry no version
information (for example a job submitted from the Monitor), which is why the unversioned
paths are listed as well; the skipped entries are reported once in the task log.

These placeholders belong to this plugin. They are deliberately *not* the Deadline path
mapping token syntax (`${type:name}`), which only applies to path mapping replacement
paths and has no token for the submitting application's version.

### Pinning a specific version

For an installation whose location does not match any template path (an unusually named
folder, a Linux tarball extracted elsewhere, a portable build), uncomment the example
block in `custom/plugins/Blender/Blender.param` — or add your own section:

```
[Blender_4.2_RenderExecutable]
Type=multilinemultifilename
Label=Blender 4.2 Executable (pinned)
Category=Render Executables
CategoryOrder=0
Index=1
Default=C:\Program Files\Blender Foundation\Blender 4.2\blender.exe
Description=Overrides the shared template for Blender 4.2 only.
```

then fill in the path in the Monitor under **Tools → Configure Plugins → Blender**. Such
a section takes precedence over the template for that version only.

## Adjusting options on an already submitted job

Everything the Blender submission dialog can set is also editable afterwards, on the
job itself, without resubmitting:

**Monitor → Jobs panel → right-click the job → Modify Job Properties → `Blender Settings`**

| Category in that dialog | Contains |
| --- | --- |
| `Scene File` | `SceneFile` |
| `Rendering Options` | `OutputFile`, `Threads`, `Build` (the stock controls) |
| `Blender Settings` | `Version`, `VersionFull`, `RenderEngine`, `RenderScene`, `ViewLayer`, `Camera`, `ImageFormat`, `ResolutionX`, `ResolutionY`, `GpuDevice`, `StrictErrorChecking`, `MarkerOverride` |

Notes:

* The Launcher itself is a tray application: its menu offers **Submit** and **Scripts**
  (the same scripts as the Monitor) but no job list. Job Properties is a Monitor dialog —
  open the Monitor from the Launcher and use its Jobs panel. The Monitor's **Submit →
  Blender** entry opens the same submission dialog described in this README, and the
  parameters it writes end up in exactly the same `Blender Settings` page.
* The controls are declared with `Required=true`, `DisableIfBlank=false` and a
  `DefaultValue`, so they are visible even for a job that was submitted with every option
  left at its default (i.e. a job whose plugin info does not contain the key at all).
  Every default means "use the value stored in the .blend file".
* Changing a value affects the tasks that this job has **not started yet**: the plugin
  reads the settings when it builds the render command line for each task. A task that is
  already rendering keeps the settings it started with. To force a job that is running to
  pick up the change, suspend and resume it, or enable **Reload Plugin Between Tasks** in
  its properties.
* Values can be changed back to "use the .blend file" at any time by setting them to
  `Use Scene Setting` (or `0` for the resolutions). Empty values are treated the same way.
* Job-level settings that are not plugin specific — frame list, frames per task, pool,
  group, priority, limits, machine list, timeouts — live in the other pages of the same
  Job Properties dialog, as they always did.
* If the `Blender Settings` group is missing and the dialog only shows `Scene File`,
  `Output`/`Threads`/`Build`, the Monitor is reading the factory
  `plugins/Blender/Blender.options` instead of this one; copy the custom file over the
  factory file (keeping this folder as the master copy) and reopen the dialog.

## Adding a Blender version

Normally nothing to do: install Blender in a conventional location and the template
above finds it (a `[Blender_RenderExecutable]` value saved in the Monitor before this
change keeps being used, because database values override the shipped default — clear
the field or add the new path there). Only if the installation does not match any
template path, follow [Pinning a specific version](#pinning-a-specific-version), then
submit a test job and check the Worker log for the `Blender render options: ...` line and
the executable that was used.

### Why some fields are drop-downs and others are not

Deadline builds that page from `custom/plugins/Blender/Blender.options`, and its control
types only support a **fixed** list of values (`Values=`/`Items=`); there is no way to fill a
list from the job or from the submitted file. So:

| Field | Control | Why |
| --- | --- | --- |
| `Render Engine` | drop-down | fixed list (`cycles`/`eevee`/`workbench`) |
| `Image Format` | drop-down | fixed list of every format Blender can store, movie formats included |
| `Cycles GPU Device` | drop-down | fixed list of the device identifiers Blender uses (**upper case**, e.g. `OPTIX`) |
| `Strict Error Checking`, `Override Camera Markers` | check box | boolean |
| `Resolution X` / `Resolution Y` | spinner | free numbers; a drop-down would be the wrong control. `0` means "keep the .blend value" |
| `Scene`, `View Layer`, `Camera` | text box | the valid names only exist inside the submitted `.blend` file, so Job Properties cannot offer a fixed list |
| `Blender Version` / `(full)` | text box | any `major.minor` is valid (the render executable template substitutes it) |
| `Scenes / View layers / Cameras in the submitted file` | read-only text | the names available at submission time, recorded by the submitter so they can be read (and copied) here |

Job Properties is the one place where those three stay text boxes. Everywhere a picker is
possible it now *is* one:

| Where | Scene / View Layer / Camera |
| --- | --- |
| Blender → *Submit To Deadline* | the render tree, filled from the file being submitted (options per combination in the table under it) |
| Monitor / Launcher → *Submit* → Blender | the render tree, filled by the **Read From File** button once the `.blend` is chosen (same reader, ~0.015 s, nothing loaded) |
| Monitor → right-click a job → *Scripts* → *Blender Render Options…* | drop-downs, filled from the job's `.blend` (see the job script section below) |

The render options themselves (`Render Engine`, `Image Format`, `Cycles GPU`, resolution,
`Strict Error Checking`, `Override Camera Markers`) are **not** individual controls in the
submission dialog any more: each ticked combination has its own row in the options table.

Job Properties cannot fill those lists itself, so the picker for an already submitted job is
a **Job script** (`custom/scripts/Jobs/`) that opens its own dialog - described next.

## Several jobs from one submission

The submission dialog submits **one job per ticked combination** in the
[render tree](#the-render-tree-and-the-per-combination-options), each with the options from its
own row. `Render One At A Time` makes every job depend on the previous one
(`-SubmitMultipleJobs -Dependent`) instead of them rendering in parallel.

Example: `shot_010`, the four combinations of `Beauty`/`Mask` x `ShotCam_0`/`ShotCam_1`,
output `.../beauty_####.png`:

| Job name | `OutputFilename0` | plugin info |
| --- | --- | --- |
| `shot_010 [Beauty / ShotCam_0]` | `.../beauty_Beauty_ShotCam_0####.png` | `RenderScene`, `ViewLayer=Beauty`, `Camera=ShotCam_0`, plus this row's options |
| `shot_010 [Beauty / ShotCam_1]` | `.../beauty_Beauty_ShotCam_1####.png` | ... |
| `shot_010 [Mask / ShotCam_0]` | `.../beauty_Mask_ShotCam_0####.png` | ... |
| `shot_010 [Mask / ShotCam_1]` | `.../beauty_Mask_ShotCam_1####.png` | ... |

* All four jobs carry `BatchName=shot_010`, so the Monitor shows them as one batch. They stay
  independent jobs: separate failure, retry, rescue and priority.
* The **view layer and camera names are appended to the output file name, in front of the frame
  numbers**, so the jobs cannot overwrite each other (`Cam A` becomes `Cam_A`; two names that
  sanitize to the same token get `_2`). This happens only for rows that kept the default path -
  a path the artist set or picked from a preset is used as it is.
* Every job takes its **own** frame list and frames per task from its row, so one camera can
  render `1-100` in chunks of 5 while another renders a single frame.
* The dialog shows no preview line of its own any more. Submitting several jobs opens a
  confirmation with **only the job count on top** (`Submits 4 jobs`) and a **read-only text box**
  below it that scrolls in both directions and lists every job with all of the options of the
  table: combination, output file, frame list, frames per task, engine, format, GPU, both
  resolutions and the two switches. A setting left at `Use Scene Setting` (or a resolution at 0)
  is shown as **the value the .blend file has for that job's scene**, so the confirmation says
  what is really going to be rendered; when the value is not known (a Monitor submission
  without a file read) it says `from the .blend file`. Notes about the chain or about
  submitting the .blend with every job are appended at the end of that text. One ticked
  combination still submits without asking.
* Every job still records the names it was submitted from (`AvailableScenes`,
  `AvailableViewLayers`, `AvailableCameras`), so the monitor job script starts up with a filled
  in dialog.

Still open (deliberately not part of this): an **assembly / post-processing job** that all the
render jobs feed into. It needs a decision about what that job runs, and the job IDs of the
render jobs (`RepositoryUtils.GetJobIds()` before/after the submission gives them), so it is
the next step rather than this one.

## Monitor job script: Blender Render Options

The dialog opens at the **smallest size it can be dragged to** (`minimumSizeHint`) and is
laid out as: a full-width **READ FROM FILE** button first (upper case, spanning all three
columns, a little taller than a normal button; its long explanation is its tooltip, because a
long label would decide the window's minimum width), then Scene, View Layer, Camera, Render
Engine, Image Format, Cycles GPU Device, Resolution X / Y (one pair of fields, flush against each
other), Strict Error Checking, Override Camera Markers below it, Blender Executable, Applies to,
and the Save / Close buttons. The status line that used to sit on top is gone, and so is the short
status line that replaced it: what was read is written to the log (`dialog opens at its minimum
size …`, `read result: …`) and the drop-downs show it.

The button carries the state of the last read: **pink** with `READ FROM FILE` while nothing has
been read, **green** with `DONE` after the names came out of the file, **red** with `FAILED` when
the file could not be read (the error dialog and the log have the reason). Pressing it again starts
from pink, so a second attempt is recognisable.

The dialog is translated as well, and it decides by the **language of the operating system**
(`CultureInfo.CurrentUICulture`, falling back to `LANGUAGE` / `LC_ALL` / `LANG`) - the Monitor has
no Blender to ask, unlike the submission dialog, which uses Blender's language. `DLB_LANGUAGE`
overrides it, which is how the tests pin a language. Translated are the labels, the check box
captions, the buttons (including `READ FROM FILE` / `DONE` / `FAILED`) and the messages; the
tooltips stay English, as in the submission dialog.

**Monitor → Jobs panel → right-click a job → Scripts → Blender Render Options…**

This is the picker that Job Properties cannot provide: real drop-downs for Scene, View Layer
and Camera, filled from the `.blend` file the job points at.

| Field | Where the values come from |
| --- | --- |
| Scene, View Layer, Camera | the name lists the submitter recorded in the job; the **Read From File** button replaces them with what is actually in the `.blend`. The view layer list follows the selected scene |
| Render Engine, Image Format, Cycles GPU Device | fixed lists, exactly the ones `Blender.options` declares |
| Resolution X / Y, Strict Error Checking, Override Camera Markers | as in Job Properties |
| Blender Executable | only used for the compressed-file fallback described below |

### The dialog never waits for the file

Opening the dialog does **no file I/O at all**: it uses the `AvailableScenes` /
`AvailableViewLayers` / `AvailableCameras` lists the submitter stored in the job, which come
straight from the database. That matters because a job's `.blend` can sit on a share that
takes minutes to answer (or is gone), and the dialog must appear immediately either way -
an earlier version read the file before showing anything and could therefore never appear.
The `.blend` itself is only read when you press **Read From File**.

## The render tree and the per-combination options

### Language

The window follows the language **Blender itself is set to**: the Blender side sends its
locale (`bpy.app.translations.locale`) in the submission context and every label of the dialog
that belongs to this script goes through `Translate()`. There is a table for Simplified and
Traditional Chinese (`zh_HANS` / `zh_HANT`) and the mechanism is a plain dict, so another
language is another dict.

Translated: the page labels, the captions of the check boxes, the tree and table headers, the
context menus, the hint line, the buttons of this script, the confirmation and the messages.
**Not** translated: the tooltips (they stay English for now), the names Deadline uses for its own
controls, and the values of the settings - `Use Scene Setting` is data that also shows up in Job
Properties, so it keeps its name. A Monitor submission has no Blender to ask and stays English.

For a test run the language can be forced with the environment variable `DLB_LANGUAGE`, e.g.
`DLB_LANGUAGE=zh_HANS`. `custom/tests/test_submission_dialog.py` builds the dialog in Chinese and
checks the translated tree header, table headers, hint and menu.

The submission dialog is built around **one tree** and **one table**:

```
[x] Scene_Main                        <- every scene in the file, each with its own names
     [x] Beauty
          [x] ShotCam_0
          [x] ShotCam_1
     [ ] Mask
          [ ] ShotCam_0
          [ ] ShotCam_1
[ ] Scene_Alt
     [ ] Layers_A
          [ ] AltCam

Combination        Render Engine      Image Format   Cycles GPU   Res X   Res Y   Strict Error   Override Markers
Beauty / ShotCam_0 [cycles        v]  [Use Scene...]  [OPTIX   v]  1920    1080    [x]            [ ]
Layers_A / AltCam  [workbench     v]  [PNG         ]  [Use Scene..] 0       0      [ ]            [ ]
```

* The tree lists **every scene of the file**, each with **its own** view layers and cameras.
  Blender sends a per-scene map, so scenes other than the one on screen can be ticked as well -
  this is what makes a multi-scene file usable in one submission. If the hint line instead says
  *"Only 1 of the N scenes could be listed …"*, the dialog was opened by an **older copy of the
  Blender-side proxy**, which could only send the active scene: re-sync `custom/submission/
  Blender/Main/SubmitBlenderToDeadline.py`, then run
  `deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\report_paths.py"` (it
  fetches the files into the repository cache and prints whether each of them is up to date).
* Every ticked **leaf** (view layer / camera) becomes one job, so the selection does not have to
  be a full cross product: this layer with both cameras and that layer with only one, in one
  submission. Ticking a view layer ticks **everything below it**, and every parent follows its
  children both ways: the view layer and the scene above a ticked camera show as
  **partially checked** straight away, become fully checked when everything below them is
  ticked, and go back to unchecked when nothing is.
* The tree opens with **the active scene's view layer and camera** ticked (the ones Blender has
  selected), so the default submission is exactly one job rendering what you see. Other scenes
  start unticked.
* **Right-click any item** (or the empty space, for the whole tree):

  | Menu entry | Effect |
  | --- | --- |
  | `Select All` | tick this item and everything below it |
  | `Select None` | untick this item and everything below it |
  | `Invert Selection` | swap ticked and unticked below this item |
  | `Select The Whole Tree` / `Clear The Whole Tree` | the same for everything |

* The line next to the tree counts what is ticked (`2 of 4 combinations ticked.`) and the
  confirmation before submitting lists the resulting job names.

**The dialog keeps its size.** The tree column and the table columns are kept at a fixed size on
purpose: sizing them to their content made the window jump wider as soon as a longer name, a
longer path or a second scene showed up. The table columns start at `TABLE_COLUMN_WIDTHS`
(`Image Format` is wider than the other two, so `Use Scene Setting` fits next to its selection
strip) and the tree column at `TREE_COLUMN_WIDTH`; the borders can still be dragged. Inside the
output cell the path field is allowed to shrink, so a narrower column shortens the text instead of
pushing the `...` button out of the cell - the button always stays on the right edge.

The Blender Options page is laid out as: *Blender File*, *Submit Blender Scene File With The Job*
directly below it, *Threads*, *Build To Force*, *Render One At A Time* above the tree, the render
tree, the options table, then - in a Monitor submission - *Read From File*, and the
**Blender Version** as the last line.

**Each ticked combination gets its own row**, so different cameras can use different output
files, frame ranges, engines, formats, resolutions or switches - the row is what that job is
submitted with. Everything the job needs is in the table; the dialog above it only describes the
submission as a whole (file, pool, priority, version, chain).

| Column | What it becomes in the job | Default |
| --- | --- | --- |
| `Combination` | read-only; `Scene / View Layer / Camera` of the row | - |
| `Output File` | `OutputFilename0` (see the naming rules below); the `...` button on its right edge opens the file dialog | the path Blender renders to, with `#` frame numbers |
| `Frame List` | `Frames` | the frame range Blender submitted |
| `Frames Per Task` | `ChunkSize` | 1 |
| `Render Engine` | `RenderEngine` (`cycles` / `eevee` / `workbench`) | `Use Scene Setting` |
| `Image Format` | `ImageFormat` | `Use Scene Setting` |
| `Cycles GPU` | `GpuDevice` (`NONE` forces CPU rendering) | `Use Scene Setting` |
| `Res X` / `Res Y` | `ResolutionX` / `ResolutionY`; both must be set or both left at 0 | **the resolution the row's scene has** |
| `Strict Error Checking` | `StrictErrorChecking` | off |
| `Override Markers` | `MarkerOverride` | off |

The resolution default is read from **that row's own scene**, so a table full of combinations
shows what each one is going to render rather than a zero. `Use Scene Setting` and `0` still mean
"keep what the `.blend` file has", so nothing is overridden unless it says so.

Rows appear and disappear with the tree selection. **A combination keeps its settings when it is
unticked and ticked again**, and ticking more cameras never resets what was already configured.

#### Selecting and changing cells

Cells are selected **one by one**, not by whole row: click a cell, or click the editor inside it
(the drop-down, the check box, the text field - an event filter turns that click into a cell
selection). `Ctrl` adds single cells to the selection, `Shift` selects the rectangular range from
the first cell, so a whole column, one cell per row, or any combination of cells can be picked.
A focus change never clears the selection - only a plain click does.

**Changing one cell changes the other selected cells of the same column**: type a frame list,
pick an engine, tick a check box or change a resolution, and every other selected cell in that
column follows. Cells in other columns are never touched, so a selection that spans several
columns keeps the values of the columns that were not edited.

A drop-down and a check box take effect immediately; a spin box as soon as its value changes; a
text field when the edit is finished - Enter, or moving to another cell.

#### Editing a cell

A click on a cell selects it **and** lets the editor handle the click itself: a text field is
ready to be typed into straight away, a drop-down opens its list, a check box toggles. `Ctrl` and
`Shift` only extend the selection - a click with a modifier never starts typing.

The three drop-down cells (`Render Engine`, `Image Format`, `Cycles GPU`) have a **blank strip on
their left**: clicking it only selects the cell - it does not open the list and does not put the
caret anywhere - which is the place to click when a cell only has to be selected, for a multi
selection or before a right-click. The list still opens when the value itself is clicked.

**Selected cells are painted in the table's highlight colour** (background and text, plus the
drop-down / spin button colours), because the table's own highlight is hidden behind the widgets
that fill the cells. Every press is written to
`%LOCALAPPDATA%\Thinkbox\Deadline10\settings\BlenderSubmission.log`
(`press row=… column=… key=… modifiers=…`), which is what to look at if a click does something
unexpected.

A press on the blank parts of a cell - the strip next to a drop-down, the padding of a container -
is **consumed** after selecting. A plain `QWidget` ignores mouse presses, so without that the
press would travel on to the table, where a `Ctrl` click on a selected cell means "remove it from
the selection"; that made Ctrl clicking the strip look as if it did nothing. It now adds to the
selection like everywhere else.

The editors carry **their own** event filters - there is deliberately no application-wide filter,
because that combination handled every click twice.

#### Right-click in the table

| Where | Menu entry | Effect |
| --- | --- | --- |
| any cell | `Reset Selected Cells To Defaults` | only the selected cells go back to their default - selecting one column resets that column and leaves the others alone |
| any cell | `Reset Selected Rows To Defaults` | the rows that have a selected cell go back to all of their defaults |
| any cell | `Reset All Rows To Defaults` | the same for every row |
| any cell | `Copy First Row To All` | repeats the first row's render options in every row - **not its output file**, which would make the jobs overwrite each other |
| the `Output File` cell | `Output File presets` | replaces that row's path with one built from its own scene / view layer / camera; the `...` button next to the path opens the normal file dialog |

A right-click that is not on the current selection means "this cell": it replaces the selection, so
the reset entries always act on what was clicked. Right-clicking the editor inside a cell (the
drop-down, the spin box, the check box, the path) opens the same menu - an event filter turns that
click into a cell selection and the menu, because the table itself never sees it.

The filter is installed on the editors **and on the parts Qt creates inside them** (the line edit
of a spin box, the view of a drop-down), because those are what receive the click and the focus;
the list a drop-down opens is left out on purpose, so choosing an item cannot replace a Ctrl
selection. None of this shows in the .job files - it only decides which cells an edit is copied
to.

The `Output File` cell also has a `...` button that opens the usual file dialog, and the presets
are offered on the path itself. They are shown as the pattern they are - `{DEFAULT_PATH}` stands
for the folder of the path the dialog was opened with - and the placeholders are filled in **per
row**, so one preset works for the whole table:

| Menu entry (default `E:\tmp5\render\beauty_####.png`) | Applied to `Scene.001 / ViewLayer / Camera` |
| --- | --- |
| `{DEFAULT_PATH}\{Scene}_{ViewLayer}\{Scene}_{ViewLayer}_{Camera}_####.png` | `E:\tmp5\render\Scene.001_ViewLayer\Scene.001_ViewLayer_Camera_####.png` |
| `{DEFAULT_PATH}\{Scene}\{ViewLayer}_{Camera}_####.png` | `E:\tmp5\render\Scene.001\ViewLayer_Camera_####.png` |
| `{DEFAULT_PATH}\{ViewLayer}_{Camera}_####.png` | `E:\tmp5\render\ViewLayer_Camera_####.png` |

* The extension of the default is kept, and the frame numbers (`####`) are part of the preset,
  so nothing else has to add them.
* **A default without an extension is treated as a folder**: `C:\tmp\####` or `C:\tmp\` becomes
  `C:\tmp` and the `#`s are dropped, so the presets are `C:\tmp\Scene_Main_Beauty\...`. Blender
  appends the frame number and the extension itself (`use_file_extension` is on).
* The Worker creates a missing output directory before rendering (Blender does not), which is
  what makes the sub folders of the presets work without preparing them by hand.
* The path of the `...` button is written into the row as chosen; the `####` of the current value
  is hidden while the file dialog is open, because it is not part of a real file name.

#### Output file naming

Job naming and output paths follow the selection: `shot_010 [Beauty / ShotCam_0]` and
`.../beauty_Beauty_ShotCam_0####.png`. When the ticked combinations come from **more than one
scene**, the scene becomes part of the job name and of the output file name too (`shot_010
[Scene_Alt / Layers_A / AltCam]`), so two scenes with a view layer or camera of the same name
cannot collide.

The view layer / camera suffix is added **only to the untouched default path**. A path the artist
typed (or picked from the presets, which already contain the names) is used exactly as it is -
otherwise `..._ShotCam_1.png` would become `..._ShotCam_1_Beauty_ShotCam_1####.png`. A **single**
ticked combination keeps the plain job name and the untouched output path.

Two rows that would write the **same** file are refused with the path spelled out, instead of
letting the jobs overwrite each other's frames.

* The tree is filled from the Blender context; when submitting from the Monitor or the
  Launcher, press **Read From File** first and the tree is rebuilt from the `.blend`
  (nothing is loaded - see below). Until then it says so instead of guessing.

Also part of this: the `BatchName` grouping, `Render One At A Time` (serial chain), the job
limit (100), the refusal to submit several jobs without an output file, and the refusal to submit
two rows that write the same file.

### What the dialog API itself offers

Worth knowing before touching this part: **Deadline has no control for a text box that is
read-only until it is clicked twice**, and no "editable table" either. The complete set of
control methods the dialog offers is (from `custom/tests/report_dialog_api.py`, run against the
live Monitor):

```
AddControlToGrid(name, control, value, row, column, tooltip='', expand=True, rowSpan, colSpan)
AddTextControlToGrid(...)            AddMultiLineTextControlToGrid(...)
AddRangeControlToGrid(...)           AddComboControlToGrid(...)
AddSelectionControlToGrid(...)       AddSubsetControlToGrid(...)
AddRadioControlToGrid(...)           AddScriptControlToGrid(...)
AddTabControl / AddTabPage / AddGroupBox / AddRow / AddGrid / AddHorizontalSpacerToGrid
```

Every one of them is a single click away from being edited; `AddTextControlToGrid` has no
read-only parameter at all. The behaviour this dialog needs - click selects, second click edits,
dropdowns and check boxes in the same grid - exists because the dialog **is** a PyQt5 window:
`AddGrid()` hands back a real `QGridLayout` and the plugins can put real `QTreeWidget` /
`QTableWidget` widgets into it. That is undocumented territory: it is true for Deadline 10.4.2.3
with Qt 5.12.12 (verified on the live Monitor), tests pin it, and if a future version changes it
the dialog falls back to "no tree, no table" with an explanation instead of breaking. Run
`report_dialog_api.py` after a Deadline upgrade to check the ground it stands on.

### Implementation note

Deadline's dialog API has no tree and no editable table, but its controls are PyQt5 widgets and
`AddGrid()` returns the real `QGridLayout`, so
`custom/scripts/Submission/BlenderSubmission.py` creates a `QTreeWidget` with checkable items
and a `QTableWidget` whose cells hold `QLineEdit` / `QComboBox` / `QSpinBox` / `QCheckBox`
widgets, and adds both to that grid. The right-click menus are `QMenu`s on the tree's and the
table's `customContextMenuRequested` signals; only the path cells have their own menu, with the
output presets and cut / copy / paste / select all actions created for it. (Taking them from `createStandardContextMenu()` is what
made the dialog crash: that temporary menu is deleted immediately and takes its actions with it.) If Qt is ever unavailable the dialog
says so and submits the ticked combinations with the `.blend` values (the tree and the table are
simply empty).

### How the names are read

`custom/lib/blend_names.py` plus the vendored parser in `custom/lib/blender_asset_tracer/`
- the same reader the submission dialog uses for its **Read From File** button:

1. **Parser (default).** It reads the file's block index and its embedded SDNA structure
   catalog. No Blender process is started and **no datablock is built** - measured on a
   15 MB file: **0.014–0.015 s**, and only a few hundred KB are read even when the `.blend`
   sits on the network share.
2. **ZStandard-compressed `.blend`.** Blender 5.2 saves `.blend` files compressed by default
   (`use_file_compression = True`; 4.5 and earlier default to off) and such a stream cannot be
   seeked, so the file is decompressed to a temporary file first - then parsed as above.
   Decompressors, first that works wins:

   1. the `zstandard` Python package, if it is importable;
   2. `compression.zstd` (Python 3.14+);
   3. **the bundled 7-Zip-zstd** in `custom/lib/7zip` - `7za.exe` on Windows and
      `7za-linux-x64` on Linux, one self-contained 1.8/2.6 MB file each, no installation, and
      independent of the Python version. **This is the one that matters here**: the Monitor and
      Worker run Deadline's own Python 3.10, which has neither of the first two, and this path
      was verified on it (see below). For macOS, upstream publishes no build, so either drop a
      zstd-capable binary in as `7za-macos-arm64`/`7za-macos-x64` or `brew install zstd`;
   4. a **`zstd` command on PATH** (`brew install zstd`, `apt install zstd`);
   5. only if all of that is missing, **the Blender executable named in the dialog** - the one
      path that opens the file and loads the scene.

   If even that is unavailable the script says so and names every option.

   Verified on Deadline's Python 3.10.18 (`deadlinecommand -ExecuteScript custom\tests\test_blend_reader.py`):
   a 15.2 MB scene compressed to 4.3 MB (19 separate zstd frames) is decompressed by the
   bundled 7-Zip to a **byte-identical** 15.2 MB file, and the names come out identical to
   what Blender itself reports, in ~0.1 s. The same binary was verified on Linux x86_64 under
   WSL, so the Windows and Linux Workers both work with no installation - see
   [custom/lib/7zip/README.md](lib/7zip/README.md) for the measurements and the macOS options.
3. The Blender executable is remembered in `<user settings>\BlenderRenderOptions.ini`;
   `BAT_BLENDER` and `BLENDER_EXECUTABLE` are honoured, and the usual
   `C:\Program Files\Blender Foundation\Blender*` installations are found automatically.

### What saving does

`RenderScene`, `ViewLayer`, `Camera`, `RenderEngine`, `ImageFormat`, `GpuDevice`,
`ResolutionX`, `ResolutionY`, `StrictErrorChecking` and `MarkerOverride` are written into
every selected Blender job and the job is saved. `Use Scene Setting` (or an empty value)
clears the key, so the value stored in the `.blend` is used again. Selecting a job of
another plugin is refused, and so is a selection spanning several different `.blend` files
while those three name fields are set - the names could not be valid for all of them.

The script only ever *reads* the `.blend`; it never saves it.

### When nothing happens

Every run appends to `%LOCALAPPDATA%\Thinkbox\Deadline10\settings\BlenderRenderOptions.log`
(the folder that holds `BlenderSettings.ini`): whether the script started, how many jobs were
selected, whether they were Blender jobs, whether the dialog was built and shown, which reader
directory was used, and how the file read went. `__main__` wraps everything, so an unexpected
error also lands in that log **and** in a dialog - an earlier version asked for
`job.JobPluginName`, a property that does not exist (the API property is `job.JobPlugin`), and
the resulting exception was visible only in the Monitor's error output: no dialog, no trace.

The other two ways a run ends without the main dialog - and both now say so in a dialog of
their own - are an empty selection (`MonitorUtils.GetSelectedJobs()` only returns *selected*
jobs, and a right-click does not always select the row) and running the script from somewhere
else than the Monitor (the Launcher's Scripts menu, `deadlinecommand`).

### "I changed a file but nothing changed"

Deadline's Repository Cache Service (RCS) serves the repository from
`%LOCALAPPDATA%\Thinkbox\Deadline10\cache\<id>\`, and it **fetches files on demand**: a file
is copied there when something asks for it. Two consequences:

* the Monitor runs the **cached** copy of a script, not the file on the share, so a change is
  only picked up once the cache syncs it again (in practice within a few minutes after the
  share was updated);
* `custom/lib` is only in the cache because our scripts ask for its files through
  `RepositoryUtils.GetRepositoryFilePath()` - a plain `import` from a `__file__`-relative path
  would fail on a machine whose cache never saw those files.

`custom/tests/report_paths.py` shows what a machine actually resolves and how fresh it is:

```
deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\report_paths.py"
```

It prints the Python version, the repository root, the resolved path and existence of every
file we ship, and - per file that decides behaviour - whether the copy the client will run
contains the current markers (**up to date** or **STALE: missing …**). Asking for a file is also
what makes the repository cache fetch it, so running this after a sync is the way to be sure the
next submission uses the new code.

### Third-party code and licence

`custom/lib/blender_asset_tracer/` is **vendored, unmodified** code from the Blender Asset
Tracer project, version 1.20, licensed **GPL-2.0-or-later** - the same licence family as the
Deadline Blender plugin that this folder customises. See
[custom/lib/README.md](lib/README.md) for the provenance, the exact file list, why 1.20 and
not the current 2.x (2.x moved dependency discovery inside Blender and therefore loads the
scene, and it requires Python 3.13), and how to update it.


## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Submission dialog looks unchanged (no `Render Engine` row) | The custom submission script is not being resolved. Run the verification script. |
| Task fails with "Could not find BlenderRenderScript.py" | The render script is missing from `custom/plugins/Blender/`, or `RepositoryUtils.GetRepositoryFilePath` is unavailable in that Worker's context — set the `Render Setup Script (optional override)` plugin configuration entry to the absolute path. |
| Task fails: `Scene 'X' does not exist` | The job references a scene that is not in the `.blend` file (usually a stale job). Resubmit. |
| GPU requested but the log says `gpu=CPU (...)` | No device of the requested type on that Worker; the fallback chain was exhausted. Install the matching driver or pin the job to GPU Workers. |
| Errors are not failing the task | Strict error checking is off by default; enable it in the dialog or set it on the job (Job Properties → Blender → Strict Error Checking). Check the pattern in `Blender_StrictErrorRegex` as well. |
| Progress no longer appears in the log | That is `[SuppressOutput]` (default `True`) — it now actually works. Set it to `False` in the plugin configuration. |
| Log says `Skipped N render executable path(s) whose version placeholder could not be filled` | The job carries no version information (typically submitted from the Monitor). The unversioned template paths are still searched; add one for your layout, or submit from Blender so the version is recorded. |
| Task fails with "Blender render executable was not found in the semicolon separated list" | No template path matched this Worker's install. Add the real path to `Blender Executable`, or pin that version with a `[Blender_<version>_RenderExecutable]` section. The log line above the failure shows which list was searched. |
| A stored executable path no longer matches what you expect | Database values win over the shipped default. Clear the `Blender Executable` field (or paste the templated value back in) so the placeholders take effect. |
| Job Properties shows `Scene File` / `Output` / `Threads` / `Build` but no `Blender Settings` group | The Monitor is reading the factory `plugins/Blender/Blender.options`. Verify the custom plugin resolves (see Verification), or copy this `Blender.options` over the factory one, keeping this folder as the master copy. |
| *Scripts → Blender Render Options* shows no window | **Select the job first** (click its row, then right-click) - `MonitorUtils.GetSelectedJobs()` only returns *selected* jobs, and a right-click does not always select. The script now says so in a dialog, and any other error is reported in a dialog too. It must also be started from the Monitor: from the Launcher's Scripts menu or `deadlinecommand` it reports that instead of doing nothing. Every run appends to `%LOCALAPPDATA%\Thinkbox\Deadline10\settings\BlenderRenderOptions.log` (next to `BlenderSettings.ini`) - that file has the reason if a window still does not appear. |
| A change to a script or option has no effect | The Monitor runs the **cached** copy from `%LOCALAPPDATA%\Thinkbox\Deadline10\cache\<id>\`. Run `deadlinecommand -ExecuteScript "<DeadlineRepository>\custom\tests\report_paths.py"`: it prints which copy is used and whether it contains the current code. |
| A parameter changed in Job Properties has no effect on a job that is already rendering | Settings are read per task: they apply to tasks that have not started yet. Suspend and resume the job, or enable `Reload Plugin Between Tasks` for it. |
| Engine / GPU / resolution changed but the render looks unchanged | Compare these task log lines: `Blender command line: ...` (does it end with `-- render=1 ...`? if not, the Worker runs an older `Blender.py`), `Blender render options: engine=...` (did the job value arrive?), `[deadline-blender-custom] options received: {...}` (did the options reach Blender?) and `[deadline-blender-custom] Rendering: engine=...` (what was actually rendered). |
| Task fails with "No render options were passed to BlenderRenderScript.py" | The script ran without the `-- render=1 ...` tokens: the Worker is running an older `Blender.py`, or the plugin could not build the command line. Compare the `Blender command line:` log line with the expected shape, then redeploy `custom/plugins/Blender/` (the Worker may also be serving a cached copy — reconnect/refresh the repository cache). |
| `options received: {...}` is there but some options are missing | The value never reached the plugin: check that the job's plugin info really contains that key (Job Properties → Blender Settings) and that the job was refreshed (`Reload Plugin Between Tasks` / suspend-resume). |
| `Rendering: engine=CYCLES ...` in the log but the job asked for EEVEE | The engine id could not be set on this Blender version (the script logs `None of the engine ids ... are available`). Check the engine ids that build supports. |
