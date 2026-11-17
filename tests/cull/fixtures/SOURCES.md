# Real-format fixture provenance

Upstream sidecars are not redistributed here. Tests that use them skip cleanly
when they are absent. To run those tests, download the files below unmodified into
`tests/cull/fixtures/downloaded/` (ignored by Git and excluded from the source
distribution) under the listed local names, and check each SHA-256 before use
(for example with `shasum -a 256` or `sha256sum`). No other test needs a network.

Two unmodified XMP files come from the official darktable integration-test
repository at pinned commit `0d3e1ac9acfbdb2c15491fd631b7f48991fcfdc9`
(raw URL: `https://raw.githubusercontent.com/darktable-org/darktable-tests/<commit>/<upstream path>`).
Total download: 51,759 bytes.

| Local name | Upstream path | XMP version | SHA-256 |
| --- | --- | --- | --- |
| `downloaded/darktable-v4.xmp` | `0001-exposure/exposure.xmp` | 4 | `a53a3be002f2d16eaca13d2bd55b59da0e1198f8a0ea6cdfb4d1683352732e2f` |
| `downloaded/darktable-v5.xmp` | `0103-almost-all/almost-all.xmp` | 5 | `cb144b680eeb5c021a397e323b0bbc65f4a59251a4c9d6a8620b53ae9069ce54` |

Source: https://github.com/darktable-org/darktable-tests/tree/0d3e1ac9acfbdb2c15491fd631b7f48991fcfdc9

The upstream repository's README instructs contributors to create these XMP files
with darktable. Version 4 includes an explicit CC-BY-SA rights statement and
publisher Pascal Obry inside the metadata; it is preserved. The repository does
not provide a top-level license, so the files are fetched locally rather than
redistributed or relicensed as MIT. The source photo is `mire1.cr2`; no RAW or
rendered image is downloaded.

Two additional original mixed-application sidecars come from KDE/digiKam commit
`33d0457e20adda97c003f3dee652a1749406ff9f`, under `core/tests/metadataengine/data/`
(raw URL: `https://raw.githubusercontent.com/KDE/digikam/<commit>/core/tests/metadataengine/data/<upstream file>`):

| Local name | Upstream file | SHA-256 |
| --- | --- | --- |
| `downloaded/digikam-raw.xmp` | `_27A1417.CR2.xmp` | `a20823ced95d6cdb7c435c31469fd96bce8a499df6bdeecf36f97573c107bab3` |
| `downloaded/digikam-jpeg.xmp` | `_27A1434.JPG.xmp` | `afa79413ab2455a7ef4e8902fe2be94b37ab4b42623c8c8246b818b0553f1067` |

These 25,674 bytes include Bibble settings/layers and legacy darktable history,
including XMP version 1 in the RAW fixture. They are multi-app fixtures found in
digiKam's upstream tests, not claimed to originate solely in digiKam. Rights text
inside the packets is preserved. As with the darktable files, they are downloaded
on demand, not redistributed under this tool's code license.

Tests independently construct left/right culling changes on these real packets
using lxml, and verify the complete history, masks, unknown fields, and original
copies. They do not run darktable or claim successful GUI reimport.

`examples/cull/*.xmp` are our own compact test data, generated with ExifTool 13.63 from
authored metadata plus an illustrative Camera Raw exposure value. They are MIT,
are not development presets, and should not be imported as edits to real photos.
The ExifTool round-trip integration test generates new packets from scratch with
the actual upstream executable, then independently reads the merged output.

`exiftool-corpus.json` pins nine original `t/images/XMP*.xmp` fixtures (18,872
bytes) at ExifTool 13.63 commit `1da714924f67763b6a8a7013dd2aa1983a16f87b`.
Each entry lists its local `name`, raw `url` and `sha256`; download them into
`downloaded/` the same way.
Seven supported packets are edited and checked for full preservation, including
unknown application fields and packet instructions. Two are deliberate rejection
cases: XMP2 has a forbidden XML control byte; XMP3 explicitly describes itself as
general RDF/XML, not strictly XMP, and contains multiple subjects. ExifTool's
permissive `-validate` accepts XMP2; `mediawrench cull` intentionally keeps strict XML
validation rather than silently repairing application sidecars.

The authored `exiftool-darktable.config` uses ExifTool's custom-namespace API to
generate and independently read a real `darktable:colorlabels/rdf:Seq` packet.
This checks the array representation against a second implementation; it does
not claim a darktable GUI reimport.
