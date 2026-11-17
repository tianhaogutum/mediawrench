# mediawrench cull: editor integration

Back to [mediawrench cull](../cull.md).

`mediawrench cull` works on three snapshots for **one photo**. Save a baseline before
working independently, or recover the actual common ancestor from your versioned
backup/synchronization history. A modification time cannot establish that ancestor.
If no baseline exists, do not invent one by choosing the newer sidecar.

1. Close the editors and pause synchronization while taking the three snapshots.
   Keep the photo itself and the snapshots separate. Use explicit file paths;
   the command does not guess between a RAW and a JPEG sharing the same stem.
2. Run `mediawrench cull plan BASE LEFT RIGHT --history-source left --format text`.
   Choose the history source according to which complete development state you
   want. It supplies all non-culling metadata, including keywords and dates.
3. For a saved review, omit `--format text`, redirect the JSON to `decisions.json`,
   and fill only its `choices` object. Otherwise use `--interactive` during merge.
   Review each conflict independently. No timestamp or higher-star preference is
   used to settle a conflict.
4. Run `mediawrench cull merge BASE LEFT RIGHT --history-source left
   --decisions decisions.json --output-dir NEW_DIRECTORY`, then
   `mediawrench cull verify NEW_DIRECTORY`. Keep that directory with its three
   originals.
5. Install `merged.xmp` under the editor's existing sidecar filename only after
   review. For darktable this is generally `photo.ext.xmp`; other editors can use
   `photo.xmp`. Determine the actual file already associated with the source photo.

## darktable

Use the same darktable version on both computers. The command does not reconcile
module-version differences or development history. Keep any referenced DTDATA
companion with the photo and the selected history source; it is never copied or
modified.

darktable keeps a database in addition to its sidecars. Its documented startup
check for updated XMP files can load the sidecar into the database; the opposite
choice overwrites the sidecar with the database state. Follow the matching version's
[multiple-computers guide](https://darktable-org.github.io/dtdocs/en/special-topics/multiple-computers/).
Test the application's reload step on a disposable copy first: darktable GUI
reimport and rendering have not been verified.

`xmp:Label` takes precedence over `darktable:colorlabels`, even for an empty or
unrecognized portable label. Read the warning when both are present. Missing
ratings also differ from portable XMP behavior: darktable can apply its configured
initial rating, so absence is compared separately for darktable packets.

## ExifTool and other applications

ExifTool can independently display the portable result:

```sh
exiftool -config '' -G1 -n -Rating -Label -Pick NEW_DIRECTORY/merged.xmp
```

For a darktable color array, the configuration file
[`tests/cull/fixtures/exiftool-darktable.config`](../../tests/cull/fixtures/exiftool-darktable.config)
lets ExifTool inspect that custom namespace
(`exiftool -config tests/cull/fixtures/exiftool-darktable.config -XMP-darktable:ColorLabels NEW_DIRECTORY/merged.xmp`).
The test suite uses it as an independent writer and reader.

ExifTool's generic rewrite is not used to create the merged result. With ExifTool
13.63, updating only `XMP-xmp:Rating` on a copy of darktable's upstream
`0001-exposure/exposure.xmp` test sidecar (`exiftool -overwrite_original
-XMP-xmp:Rating=4 copy.xmp`) warns `Ignored empty rdf:Seq list for
darktable:masks_history`, removes that empty list, and rewrites the history resource
syntax throughout the packet. The file stays well-formed and the history entries
remain, but that is incompatible with a promise to leave every non-culling property
structurally intact. `mediawrench cull` instead updates its small allowlist with lxml
and compares every non-culling property before publishing an output bundle.

digiKam and other editors may keep several rating/color aliases. Known aliases
outside the allowlist are reported and retained from the history source. Check the
actual application's display before resuming synchronization; selecting one
metadata representation does not convert every application's color or pick
vocabulary. Vendor flag encodings stay independent.
