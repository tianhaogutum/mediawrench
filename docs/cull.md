# mediawrench cull

When you cull the same photos on two computers, their XMP sidecars drift apart:
one side changes the stars, the other a color label or a pick flag, and file
synchronization can only keep one whole sidecar, losing the other side's decisions.
`mediawrench cull` takes the photo's last common-ancestor sidecar and the two
changed copies, merges **only** stars, color labels and pick flags with an
explicit three-way decision per field, and keeps every other XML property
(development history, masks, keywords, copyright, unknown namespaces) from one
sidecar you choose. Use it to reconcile culling done in darktable, digiKam or any
other XMP-writing editor before you resume synchronization.

It is a small adapter around [lxml](https://lxml.de/) and
[simplejson](https://github.com/simplejson/simplejson), not a photo editor or
synchronizer. It never opens RAW images or application databases, runs a server or
contacts a network, and it never overwrites its inputs.

```bash
pip install 'mediawrench[cull]'
mediawrench cull --help
```

The `cull` extra installs lxml and simplejson. Without it, `mediawrench cull --help`
still lists the subcommands and prints the install hint; every other invocation exits
with code 2 and the same hint.

## Quick start

The examples are three small synthetic sidecars in
[`examples/cull/`](../examples/cull/). Run from a clone of the repository.

```bash
mediawrench cull plan examples/cull/base.xmp examples/cull/left.xmp examples/cull/right.xmp \
  --history-source left > decisions.json

mediawrench cull plan examples/cull/base.xmp examples/cull/left.xmp examples/cull/right.xmp \
  --history-source left --format text

mediawrench cull merge examples/cull/base.xmp examples/cull/left.xmp examples/cull/right.xmp \
  --history-source left --choose rating=right --choose pick=left \
  --output-dir demo-output

mediawrench cull verify demo-output
```

The example has two genuine conflicts: stars (3 versus 5, ancestor 1) and pick
(1 versus -1, ancestor 0). The right side's color label (`Red` to `Green`) is the
only change to that field and merges automatically. The left side's development
settings (the illustrative `crs:Exposure2012` value 0.75) remain selected. Every
original stays untouched. The text review names each field and its status, for
example:

```text
rating [CONFLICT]: base=1  left=3  right=5
label [right_changed]: base="Red"  left="Red"  right="Green"
pick [CONFLICT]: base=0  left=1  right=-1
...
Choices required: rating, pick
```

To decide interactively, replace the `--choose` arguments with `--interactive`.
Each unresolved field is shown separately with ancestor/left/right values. Enter
`base`, `left`, `right` or `quit`; there is no automatic winner for a conflict.

For offline review, fill the `choices` object in `decisions.json` with side names
and pass `--decisions decisions.json` to `merge`. The tool verifies all input
SHA-256 hashes and the selected history source, refusing decisions for files changed
since review. Do not edit any other part of the plan to change a result; only
`choices` is read.

For baseline snapshots, editor reload and independent ExifTool inspection, see
[Editor integration](cull/integration.md).

## Commands

```text
usage: mediawrench cull [-h] [--version] {plan,merge,verify} ...
```

| Subcommand | Does |
| --- | --- |
| `plan BASE LEFT RIGHT --history-source SIDE [--format json\|text]` | Inspect differences; print a reviewable JSON decision plan (default) or a compact text review. Writes nothing. |
| `merge BASE LEFT RIGHT --history-source SIDE --output-dir DIR [--choose FIELD=SIDE ...] [--decisions PLAN.json] [--interactive]` | Write a new sidecar, an audit and three original copies into a new directory. |
| `verify DIR` | Check a saved bundle without the original input paths. |

`BASE` is the last common ancestor, `LEFT` and `RIGHT` are the sidecars from the two
computers. `--history-source base|left|right` is required for `plan` and `merge`: that
input supplies **all** non-culling data; history is never merged. `--choose` resolves
one conflicting field and may be repeated; it can be combined with `--decisions`
(a field may be chosen only once) and `--interactive` (which then asks only for the
remaining conflicts and requires a terminal). Choosing a field that is not a conflict
is an error. `--version` prints `mediawrench cull 0.1.0`.

stdout is JSON (or the text review); diagnostics, warnings and prompts go to stderr.
Exit codes: `0` for a successful plan, merge or verification; `2` for invalid inputs,
unresolved conflicts, explicit review cancellation or write errors; `130` for Ctrl-C.
JSON escapes non-ASCII characters for console and redirection compatibility without
changing the underlying metadata values.

## Output and merge rules

The output directory must not exist, even if empty. Its parent must already exist.
It contains:

- `merged.xmp`: the new sidecar, based on `--history-source base|left|right`.
- `originals/base.xmp`, `left.xmp`, `right.xmp`: exact input bytes.
- `audit.json`: input/output SHA-256, per-field values and decisions, warnings,
  history source, and the verified non-culling structure fingerprint.

With ancestor B and versions L/R: if L=R, use that agreement; if L=B, use R;
if R=B, use L. Every other case needs a separate explicit decision. Missing values
are JSON `null`. The XMP standard defines a missing rating as unrated (`0`), so
those compare equally for portable XMP. When a `darktable:xmp_version` property is
present, absence is compared separately because darktable imports it using a
configurable initial rating rather than necessarily zero. The policy is recorded
in the plan and audit (`rating_absence_policy`: `portable_unrated` or
`darktable_import_default`). The chosen history source's representation is retained
when equivalent. Empty label `""` and empty color set `[]` remain distinct from
missing properties. Deleting a property on one side is otherwise a real change.
Color sets compare without order or duplicate entries; a conflict is resolved as
a whole set, never by silent union.

Supported direct properties of the photo's `rdf:Description`:

| Field | XMP property | Values |
| --- | --- | --- |
| `rating` | `xmp:Rating` | -1 (reject), or a real number in [0, 5]; absence follows the policy above |
| `label` | `xmp:Label` | arbitrary text, including localized/custom names |
| `darktable_labels` | `darktable:colorlabels` | `rdf:Seq` of 0 red, 1 yellow, 2 green, 3 blue, 4 purple |
| `pick` | `xmpDM:pick` | -1 reject, 0 unflagged, 1 picked |
| `digikam_pick` | `digiKam:PickLabel` | 0 none, 1 rejected, 2 pending, 3 accepted |
| `digikam_color` | `digiKam:ColorLabel` | 0 none, 1 red, 2 orange, 3 yellow, 4 green, 5 blue, 6 magenta, 7 gray, 8 black, 9 white |

Fractional ratings use exact decimal arithmetic and exact JSON numbers; values
that differ beyond binary floating-point precision still require review. Some
editors display only whole stars even when a sidecar contains a fractional rating.

Scalar attributes and scalar child elements are accepted, regardless of namespace
prefix. Multiple descriptions of the same RDF subject are supported. Duplicate
definitions or qualified/structured culling properties are refused as ambiguous.
Vendor pick encodings remain separate; no vendor encoding is ever converted into
another. Keyword/tag collections (`dc:subject`, `lr:hierarchicalSubject`) remain
untouched.

The plan and audit are JSON with `"schema_version": 1`. They also record a `tool`
value of `cullmerge 0.1.0`, the name of the standalone tool this command came from;
it is kept unchanged so plans and bundles written by either remain interchangeable.

## Preservation and recovery

The selected input supplies **all** non-culling data, not only history. A
namespace-aware structural fingerprint checks every unselected property after
serialization, including unknown fields, empty arrays and history entry order.
XML formatting/quoting may change; protected namespace bindings are checked.
Byte-for-byte copies are in `originals/`. If no field changes on the selected
input, output bytes are identical.

XMP reads are limited to 16 MiB per file and parsing stops at 100,000 XML elements or
300,000 attributes. XML DTDs/entities and unsupported RDF shapes are refused.
UTF-8, UTF-16 and UTF-32 XML packets are supported; edited output uses UTF-8.
Decision/audit JSON is capped at 1 MiB, with UTF-8 BOM and UTF-16/32 review files
supported for text-editor interoperability; duplicate JSON keys and non-finite
numbers are refused.

Failed or cancelled writes clean up their new output directory. A hard kill or power
failure can leave an `.incomplete` marker; a bundle without `audit.json` is not
complete. Inputs remain untouched and can be retried with a fresh destination.
`mediawrench cull verify OUTPUT_DIR` checks the original copies, the merged SHA-256,
the recorded differences/choices and the non-culling preservation invariant. It
works after moving a bundle or deleting the original input paths. Verification
detects corruption and inconsistent edits; the audit is not a signed certificate of
authenticity. Keep an independent backup when that matters.

## Limitations and application use

- Supply the actual last common ancestor and sidecars for the same source photo.
  Shared original IDs/raw names are compared when available. Missing identity
  evidence produces a warning; XMP cannot prove which RAW a file belongs to.
  Matching filenames are weaker evidence than a shared stable original ID.
  Folder separators and file URLs are normalized for filename hints; a legitimate
  rename with a shared original ID produces a warning instead of a false refusal.
- darktable reads `xmp:Label` before its own color set. Both are separate fields;
  a warning explains precedence if both survive. No color-label translation is
  guessed. A rejected rating plus a picked flag also produces a warning.
- Vendor rating aliases outside the allowlist are reported when present.
  digiKam can mirror color into `photoshop:Urgency`; that field stays with the
  chosen history source because it can also mean editorial urgency. Inconsistent
  known digiKam/portable color or pick decisions are reported without conversion.
- History is selected wholesale, never reconciled. Differences in history versions,
  module parameters, masks or referenced DTDATA files need the editor's own care.
  DTDATA companions, RAWs and catalog databases are never copied or merged.
- A live editor can overwrite sidecar changes from its database. Close the editor
  before replacing a sidecar, keep the bundle, and use the application's documented
  metadata-reload/reimport flow. Application GUI reimport has not been tested.
  Non-culling dates such as `xmp:MetadataDate` are also retained from the chosen
  source; no automatic timestamp or catalog synchronization is performed.
- No directory-wide matching, embedded XMP, cross-application flag conversion or
  custom culling keywords. Inputs use one RDF subject and simple culling values.
- Legacy packet wrappers declaring a fixed byte count or a non-UTF-8 encoding hint
  can be inspected/copied unchanged but are refused for edits. Normalize those
  wrappers with the original editor on copies; the command cannot change them while
  promising to preserve unrelated packet instructions.

Tested on macOS with Python 3.11, 3.12 and 3.15. Linux and Windows application
workflows are not verified. ExifTool is optional and used only by independent
integration tests; the command itself never runs it. Those tests, and tests on real
darktable/digiKam/ExifTool sidecars, skip when ExifTool (`EXIFTOOL=/path/to/exiftool`
or `exiftool` on `PATH`) or the downloaded sidecars are unavailable; how to obtain the
pinned upstream sidecars is described in
[`tests/cull/fixtures/SOURCES.md`](../tests/cull/fixtures/SOURCES.md).

## Related tools

Existing tools cover neighboring needs but not an ancestor-based, per-field review
of two independently changed culls with a preserved, explicitly chosen history:

- [ExifTool](https://exiftool.org/) reads and writes all of these properties and is
  the recommended independent checker, but rewriting a sidecar with it reformats
  the whole packet and can drop empty unknown RDF lists (for example darktable's
  `darktable:masks_history`); see [Editor integration](cull/integration.md).
- [darktable](https://www.darktable.org/) documents that it has no built-in conflict
  resolution between computers: database/XMP synchronization picks one complete state.
- Photo managers such as Facet document newest-wins sidecar handling, and LightCraft
  preserves existing XMP while updating its own properties; neither establishes a
  common ancestor or asks about opposing edits.
- [Python XMP Toolkit](https://python-xmp-toolkit.readthedocs.io/) reads and writes
  XMP properties through Exempi but has no three-way decisions or audit bundles.
