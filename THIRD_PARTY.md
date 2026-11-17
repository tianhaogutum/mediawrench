# Third-party sources and attribution

mediawrench is licensed under MIT (see [LICENSE](LICENSE)). The commands below were
previously separate MIT-licensed projects by the same authors; their notices are
preserved below, one section per command, with paths updated to this repository's
layout. Dependencies are installed by your package manager under their own licenses
and are never vendored; the mediawrench wheel contains only the `mediawrench` package
(including a small, unmodified MIT subset of ChronoSync used by `drift`, see
[mediawrench drift](#mediawrench-drift)) and these project notices. License texts that
accompany redistribution are in [licenses/](licenses/): pysubs2, wcwidth and srt (MIT),
json5 (Apache-2.0) and ChronoSync (MIT).

Dependency versions quoted below are the ones each tool was originally locked and
validated against; mediawrench itself declares version ranges in `pyproject.toml`.
Product names identify interoperable formats and programs only; mediawrench is not
affiliated with or endorsed by FFmpeg, Audacity, LosslessCut, OpenAI, yt-dlp,
MKVToolNix, ChronoSync, darktable, digiKam, Adobe, ExifTool or any other project named here.

## At a glance

### Runtime dependencies (installed separately, never vendored)

| Package | License | Used by |
| --- | --- | --- |
| [pysubs2](https://github.com/tkarabela/pysubs2) | MIT ([text](licenses/pysubs2-LICENSE.txt)) | `rollup`, `reflow`, `audacity`, `speakers`, `cutlist`; `chunks` tests |
| [wcwidth](https://github.com/jquast/wcwidth) | MIT ([text](licenses/wcwidth-LICENSE.txt)) | `reflow` (grapheme and display-width measurement) |
| [srt](https://github.com/cdown/srt) | MIT ([text](licenses/srt-LICENSE.txt)) | `cutsync` (SRT parsing and serialization) |
| [json5](https://github.com/dpranke/pyjson5) | Apache-2.0 ([text](licenses/json5-LICENSE.txt)) | `cutsync` (LosslessCut `.llc` JSON5 projects) |
| [NumPy](https://numpy.org/), [SciPy](https://scipy.org/) | BSD-3-Clause | `drift` (optional `drift` extra): numerical work, FFT and peak search |
| [python-soxr](https://github.com/dofuuz/python-soxr) | LGPL-2.1-or-later (wraps libsoxr, LGPL-2.1-or-later) | `drift` (optional `drift` extra): streaming float-rate resampling |
| [SoundFile](https://github.com/bastibe/python-soundfile) | BSD-3-Clause (wraps libsndfile, LGPL-2.1-or-later) | `drift` (optional `drift` extra): WAV/RF64 output |
| [lxml](https://lxml.de/) | BSD-3-Clause (binary wheels also include libxml2/libxslt, MIT, and other libraries under their own notices) | `cull` (optional `cull` extra): XMP parsing and serialization |
| [simplejson](https://github.com/simplejson/simplejson) | MIT OR AFL-2.1 | `cull` (optional `cull` extra): exact-decimal JSON plans and audits |

### External programs (never bundled, downloaded or installed by mediawrench)

| Program | License | Used by |
| --- | --- | --- |
| [FFmpeg](https://ffmpeg.org/) `ffmpeg` and `ffprobe` | LGPL-2.1-or-later; GPL builds when configured with GPL components (see each build's notices) | `loudness`, `postflight`, `drift` (required); `chapters verify-media`, `cutlist --ffprobe` (optional) |
| [ExifTool](https://exiftool.org/) | Same terms as Perl (Artistic License or GPL) | optional `cull` interoperability tests only; never run by the command |

mediawrench runs these as separate processes with explicit argument lists. Whichever
build you install keeps its own license; using it through mediawrench does not
relicense either side.

Test-only dependencies (`test` extra) are [pytest](https://github.com/pytest-dev/pytest) (MIT) and its
dependencies (iniconfig, pluggy: MIT; packaging: Apache-2.0 OR BSD-2-Clause; Pygments: BSD-2-Clause),
[hypothesis](https://github.com/HypothesisWorks/hypothesis) (MPL-2.0) and the packages of the
`drift` and `cull` extras listed above.
Build-only tools are [hatchling](https://github.com/pypa/hatch) (MIT) and
[hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) (MIT).

### Bundled code and data

- **One third-party source subset is vendored:** four unmodified ChronoSync modules (MIT) in
  `src/mediawrench/drift/_chronosync/`, because ChronoSync is not published on PyPI. Every
  other module under `src/mediawrench/` is original MIT work.
- **No third-party media, models or datasets are bundled.** All captions, transcripts, RTTM,
  chapter sidecars and media fixtures under `examples/`, `tests/` and `benchmarks/` are
  original synthetic data under MIT; audio and video test inputs are generated at test time
  with FFmpeg's built-in `lavfi` sources or Python's `wave` module, except the four small
  synthetic MP4 fixtures in `tests/cutlist/fixtures/` (black frames and silence encoded
  locally with FFmpeg; they contain media data only, no FFmpeg code, and are covered by the
  root MIT license; see its README). The `drift` tests generate their speech-like audio with
  FFmpeg's `anoisesrc`; the `cull` examples are original synthetic XMP sidecars.
- **Exception, test data only:** `tests/cutsync/fixtures/losslesscut/` holds four unmodified
  LosslessCut parser test fixtures (`sample.srt`, `test1.csv`, `test2.csv`, `test3.csv`)
  under GPL-2.0, with that license beside them. They ship in the sdist with the tests,
  not in the wheel, and no LosslessCut code is included. Optional upstream XMP sidecars
  for the `cull` tests are downloaded by hand into an ignored directory and never
  distributed (see [mediawrench cull](#mediawrench-cull)).
- FFmpeg itself is an external program that you install separately. It is not bundled in
  the wheel, the sdist or the repository, and mediawrench never downloads it.

## Contents

- [mediawrench rollup](#mediawrench-rollup)
- [mediawrench reflow](#mediawrench-reflow)
- [mediawrench audacity](#mediawrench-audacity)
- [mediawrench cutsync](#mediawrench-cutsync)
- [mediawrench asr-audit](#mediawrench-asr-audit)
- [mediawrench speakers](#mediawrench-speakers)
- [mediawrench cutlist](#mediawrench-cutlist)
- [mediawrench chunks](#mediawrench-chunks)
- [mediawrench loudness](#mediawrench-loudness)
- [mediawrench postflight](#mediawrench-postflight)
- [mediawrench chapters](#mediawrench-chapters)
- [mediawrench drift](#mediawrench-drift)
- [mediawrench cull](#mediawrench-cull)

## mediawrench rollup

Formerly the standalone MIT project caption-rollup-repair. Its implementation
(`src/mediawrench/rollup/`), tests (`tests/rollup/`), documentation and synthetic
captions (`examples/rollup/`) are original MIT work. No competitor implementation
was copied.

### Runtime dependency

[pysubs2](https://github.com/tkarabela/pysubs2), MIT (originally locked and
validated at version **1.8.0**). It writes the SRT output and verifies each
generated SRT by reading it back. The notice below is verbatim from the 1.8.0
wheel:

> Copyright (c) 2014-2024 Tomas Karabela
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
> THE SOFTWARE.

The original project's build tools were `setuptools` (MIT) and `wheel` (MIT);
their distributions keep their own notices and nothing from them is copied into
the implementation.

### Format interoperability and related projects

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) is an upstream producer of saved VTT.
  Its source is Unlicense; packaged binaries have additional dependency licenses.
  mediawrench does not import, execute, download or redistribute yt-dlp.
- [srt_fix](https://github.com/bindestriche/srt_fix) already addresses duplicate SRT
  lines, as a postprocessor and standalone GUI. Basic duplicate removal is not a
  new invention; `mediawrench rollup` focuses on local WebVTT, adjacent
  temporal/whole-line evidence, ambiguity reporting and a per-token provenance
  map. There is no general superiority claim and no source-code reuse.
- The [WebVTT specification](https://www.w3.org/TR/webvtt1/) describes the
  interoperable format; the supported subset is documented in
  [docs/rollup.md](docs/rollup.md). Background also includes
  [yt-dlp issue 1734](https://github.com/yt-dlp/yt-dlp/issues/1734).
  Specifications retain their own notices.

## mediawrench reflow

Formerly the standalone MIT project wordcue-reflow. Its implementation
(`src/mediawrench/reflow/`), tests (`tests/reflow/`), synthetic examples
(`examples/reflow/`) and benchmark (`benchmarks/reflow/`) are original MIT work.
No media or model is included.

### Runtime dependencies

- [pysubs2](https://github.com/tkarabela/pysubs2), MIT (originally locked and
  validated at version **1.9.1**). Copyright (c) 2014–2026 Tomas Karabela. Used
  for real SRT serialization and round-trip parsing. The full notice is retained
  in [licenses/pysubs2-LICENSE.txt](licenses/pysubs2-LICENSE.txt). No
  implementation source was copied.
- [wcwidth](https://github.com/jquast/wcwidth), MIT (originally locked and
  validated at version **0.9.4**, whose bundled Unicode tables report version
  18.0.0). Copyright (c) 2014 Jeff Quast. Used through `iter_graphemes` and
  `wcswidth(ambiguous_width=1)` for extended grapheme boundaries and display-cell
  estimates; no terminal-specific correction profile is used. The full notice is
  retained in [licenses/wcwidth-LICENSE.txt](licenses/wcwidth-LICENSE.txt). No
  implementation source was copied.

The original project's build tools were pip, setuptools and wheel (all MIT);
their distributions keep their own notices and are not bundled.

### Format interoperability only

- [openai/whisper](https://github.com/openai/whisper), MIT: existing JSON input.
- [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper), MIT:
  serialized segment/word dictionaries (live Python generator objects are not
  accepted).
- [m-bain/whisperX](https://github.com/m-bain/whisperX), BSD-2-Clause: existing
  JSON segment words and flattened `word_segments`.
- [jianfch/stable-ts](https://github.com/jianfch/stable-ts): related project; its
  regrouping and subtitle export already exist. No code is imported or copied
  from it, and no novelty or license claim is made about its implementation.

No model or upstream executable is installed or redistributed. Format
interoperability does not relicense these projects.

## mediawrench audacity

Original implementation (`src/mediawrench/audacity/`), examples, tests and
`benchmarks/audacity/benchmark.py` are original MIT work; all subtitle fixtures are
synthetic. No Audacity, W3C or other upstream implementation is copied or linked.

### Runtime dependency: pysubs2 (MIT)

- Project: https://github.com/tkarabela/pysubs2 (originally validated with 1.8.0,
  wheel SHA-256 `05716f5039a9ebe32cd4d7673f923cf36204f3a3e99987f823ab83610b7035a0`).
- Installed separately by your package manager, not vendored. Used to parse actual SRT
  and to produce its numbering/timestamp scaffold; original raw payloads bypass SSA text
  normalization.

Notice (also retained in the installed distribution):

> Copyright (c) 2014-2026 Tomas Karabela
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
> THE SOFTWARE.

### Interoperability references (no linked or copied code)

- Audacity: https://github.com/audacity/audacity (GPL-3.0 release; many individual
  source files GPL-2.0-or-later). Only UTF-8 tab-separated label-file interoperability is
  implemented. No Audacity source, executable or GUI automation is bundled.
- Audacity Manual label-format reference:
  https://raw.githubusercontent.com/audacity/audacity-manual/master/manual/man/importing_and_exporting_labels.html
  (credits the Audacity Team, CC BY 3.0). No document text is copied into the
  implementation.
- Issue report: https://github.com/audacity/audacity/issues/6186, describing
  multiline/WebVTT limitations; user-authored issue material remains third-party and is
  not proof of current GUI behavior.
- WebVTT: https://www.w3.org/TR/webvtt1/ (W3C document license). Cue-setting authoring
  syntax follows the W3C Candidate Recommendation of 4 April 2019, sections 4.1 and 4.4:
  https://www.w3.org/TR/2019/CR-webvtt1-20190404/#webvtt-cue-settings. The validator,
  serializer and synthetic settings fixtures are original code; no W3C document or
  implementation is bundled.

## mediawrench cutsync

Formerly the standalone MIT project srt-cut-sync ("Copyright (c) 2026 tianhaogutum"). Its implementation (`src/mediawrench/cutsync/`), tests
(`tests/cutsync/`), documentation and authored examples (`examples/cutsync/`) are
original MIT work. It is a thin integration around `srt` and `json5`: it reuses their
parsers and serializer unmodified (no fork or monkeypatch) and adds only the
piecewise keep-list time mapping and its per-cue audit. No competitor implementation
was copied.

| Project | Version / license | Relationship |
| --- | --- | --- |
| [srt](https://github.com/cdown/srt) | MIT (originally validated with 3.5.3) | Runtime SRT parsing, timestamp serialization and cue objects. Installed separately, not vendored; license text in [licenses/srt-LICENSE.txt](licenses/srt-LICENSE.txt) ("Copyright (c) 2014-present Christopher Down"). |
| [json5](https://github.com/dpranke/pyjson5) | Apache-2.0 (originally validated with 0.17.3) | Runtime parsing of LosslessCut `.llc` JSON5 projects. Installed separately, not vendored; license text in [licenses/json5-LICENSE.txt](licenses/json5-LICENSE.txt). |
| [LosslessCut](https://github.com/mifi/lossless-cut) | GPL-2.0 | Interoperability with its public CSV (seconds and frames) and `.llc` v1/v2 project formats, read from source commit `4c7ab1cb0c9acaefabb48e997bc909562dc3624d` (`edlFormats.ts`, `edlStore.ts`, `types.ts`, `getTimeFromFrameNum`, `formatCsvFrames`, `util/duration.ts`); no implementation, binary or GUI is distributed. Four unmodified test fixtures are included as test data (below). |
| [pysubs2](https://github.com/tkarabela/pysubs2) | MIT (1.9.1 surveyed) | Related project / reference only (subtitle conversion, shifting, FPS transforms); not used by `cutsync`. |
| [srt-cut](https://github.com/overlookmotel/srt-cut) | 2.0.0 surveyed | Related project / reference only; no code or dependency. |
| [Auto-Editor](https://github.com/WyattBlue/auto-editor), [yt-dlp](https://github.com/yt-dlp/yt-dlp) | source surveyed 2026-11-11 | Related projects that already remap subtitle sidecars inside full editing/download workflows; reference only. |
| [FFmpeg](https://ffmpeg.org/) | LGPL-2.1+ or GPL, depending on the build | Not used at runtime. FFmpeg 9.0.2 was used once to convert the authored `tests/cutsync/fixtures/editor-export.ass` into `ffmpeg-export.srt`; tests optionally run `ffprobe` on an output SRT when it is on `PATH`. |

Test fixtures in `tests/cutsync/fixtures/`: `losslesscut/sample.srt`, `test1.csv`,
`test2.csv` and `test3.csv` were downloaded unchanged on 2026-11-11 from
`https://github.com/mifi/lossless-cut/tree/4c7ab1cb0c9acaefabb48e997bc909562dc3624d/src/renderer/src/test/fixtures`
and keep their upstream GPL-2.0 license, included beside them as
`tests/cutsync/fixtures/losslesscut/LICENSE`; they are test data only and are not in the
wheel. `editor-export.ass` is authored (MIT) and `ffmpeg-export.srt` is its FFmpeg 9.0.2
conversion. Details are in `tests/cutsync/fixtures/PROVENANCE.md`. The `.llc` examples
follow the schema and serializer read from the pinned LosslessCut source but were not
saved through the LosslessCut GUI.

Development of the original project used pytest, hypothesis, ruff, build and hatchling
(none bundled or needed at runtime). Upstream behaviors it works around (srt
regular-expression backtracking on short malformed input; json5 0.17.3 leaving escaped
UTF-16 surrogate pairs uncombined; the final-timeline mapping question in LosslessCut
issue 3209) are documented in `docs/cutsync.md`; nothing was reported upstream.

## mediawrench asr-audit

Original implementation (`src/mediawrench/asraudit/`), examples, tests, fixtures and the
benchmark under `benchmarks/asr-audit/` are original MIT work. No upstream implementation,
model, media or competitor code is included. Runtime dependencies: none (standard library
only). All examples and fixtures are synthetic.

### Format relationships (no model or decoder code)

| Project | Upstream license | Relationship |
| --- | --- | --- |
| [openai/whisper](https://github.com/openai/whisper) | MIT | Reads the segments/start/end/text JSON subset and optional segment scores; other metadata is retained. |
| [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Accepts segment records explicitly serialized to the same seconds-based schema. Word data is retained as metadata, not audited. This is not a universal faster-whisper JSON export claim. |
| [ggml-org/whisper.cpp](https://github.com/ggml-org/whisper.cpp) | MIT | Explicit opt-in adapter for the v1.7.6 CLI `-oj`/`-ojf` segment timing schema at commit `a8d002cfd879315632a579e73f0148d06959de36`. Integer millisecond offsets are checked against comma timestamp strings before conversion. Original synthetic offline fixtures and pinned exporter provenance are in `tests/asraudit/fixtures/`; no upstream code or model output is bundled. |

Format interoperability does not relicense upstream projects. The upstream license
notices of whisper, faster-whisper and whisper.cpp were reviewed when the tool was
designed; none of their files are redistributed here.

### Competitor boundary

[whisper-hallucination-remover](https://github.com/godxxy1229/whisper-hallucination-remover)
is a prior removal project, referenced only as related work; its license was not
verified. No code or assets were copied and no removal behavior is implemented. Issue
reports cited in [docs/asr-audit.md](docs/asr-audit.md) are motivation, not ground truth
or evidence of current upstream defects.

## mediawrench speakers

Formerly the standalone `interview-speaker-bridge` project (MIT). The implementation
under `src/mediawrench/speakers/` and the synthetic examples in `examples/speakers/`
are original MIT work. No model, competitor implementation, media or external
executable is bundled.

### Runtime dependency

[pysubs2](https://github.com/tkarabela/pysubs2) (MIT; originally validated with
1.8.0) is used for real SRT serialization and reparse validation. It is installed
separately and not vendored; its license text is in
[licenses/pysubs2-LICENSE.txt](licenses/pysubs2-LICENSE.txt). The notice reproduced
from the `pysubs2-1.8.0.dist-info/LICENSE.txt` wheel the tool was validated with reads:

> Copyright (c) 2014-2024 Tomas Karabela
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
> THE SOFTWARE.

### Build tooling

The original project was built with setuptools 80.9.0 (MIT) and bootstrapped with
pip 25.3 (MIT, with its bundled components' notices in its wheel). Neither is
imported at runtime or included in the distribution.

### Format interoperability and related projects

- [openai/whisper](https://github.com/openai/whisper): MIT. Existing segment/word
  JSON compatibility only; no model loading or code copied.
- [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper): MIT.
  Compatibility with a JSON serialization of documented segment and word fields,
  not a claim of an official universal faster-whisper JSON export schema.
- [m-bain/whisperX](https://github.com/m-bain/whisperX): BSD-2-Clause. Existing
  alignment JSON compatibility only; no code copied. WhisperX already offers
  alignment and speaker assignment.
- [MahmoudAshraf97/whisper-diarization](https://github.com/MahmoudAshraf97/whisper-diarization):
  cited as an existing ASR/diarization pipeline, not imported or distributed.
- RTTM: standard ten-field `SPEAKER` records, interpreted as seconds.

The original project's research notes (issue captures, upstream documentation and
license snapshots, including a newer upstream pysubs2 license with a later
copyright year) are not part of this repository; those third-party materials keep
their original rights and are not relicensed.

## mediawrench cutlist

Formerly the standalone `transcript-cutlist` project (MIT). The implementation under
`src/mediawrench/cutlist/`, the synthetic examples in `examples/cutlist/` and the
benchmark in `benchmarks/cutlist/` are original MIT work. No competitor
implementation is copied or bundled.

| Project | Version / license | Relationship |
| --- | --- | --- |
| [pysubs2](https://github.com/tkarabela/pysubs2) | MIT (originally validated with 1.8.0) | Runtime SRT parsing and SRT round-trip tests. Installed separately, not vendored; license text in [licenses/pysubs2-LICENSE.txt](licenses/pysubs2-LICENSE.txt). |
| [LosslessCut](https://github.com/mifi/lossless-cut) | GPL-2.0 (or-later applicability unverified) | Interoperability with its public `.llc` v2 project and seconds CSV formats only; no implementation, binary or GUI is distributed. |
| [autocut](https://github.com/mli/autocut) | Apache-2.0 | Related project / reference only; no code or dependency. |
| [FFmpeg](https://ffmpeg.org/) `ffprobe` | LGPL-2.1+ or GPL, depending on the build | Optional external executable named explicitly with `--ffprobe`; never bundled, downloaded or searched for on `PATH`. |

The test media in `tests/cutlist/fixtures/` (`origin-zero.mp4`, `origin-one.mp4`,
`origin-multistream.mp4`, `rates-multistream.mp4`) are project-created synthetic
black-frame/silence files generated locally with FFmpeg from `lavfi` sources; no
external media was used and they are covered by the root MIT license. Generation
commands and hashes are in `tests/cutlist/fixtures/README.md`.

The original project was built with pip 25.3, setuptools 80.9.0 and wheel 0.45.1
(all MIT; pip with its own vendored notices); uv 0.11.19 was used only to populate a
local virtual environment. None of these is bundled or needed at runtime. Only
public format documentation was used for interoperability. The original project's
research notes (format references and issue captures) are not part of this
repository; those third-party materials keep their original rights and are not
relicensed.

## mediawrench chunks

Formerly the standalone `audio-chunk-manifest` tool. The implementation in
`src/mediawrench/chunks/`, its tests (`tests/chunks/`), the synthetic demo
(`examples/chunks/`) and benchmark (`benchmarks/chunks/`) are original MIT work. No
competitor implementation, model, binary or audio recording is copied or bundled.
Runtime uses only the Python standard library.

| Reference | License / role | Relationship |
| --- | --- | --- |
| [openai/whisper](https://github.com/openai/whisper) | MIT | Offline `segments` / `words` JSON interoperability only. No import, model or API. |
| [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Interoperates with caller-serialized segments and words, not Python generator/pickle objects. No import or inference. |
| [jiaaro/pydub](https://github.com/jiaaro/pydub) | MIT (per the original research metadata) | Existing slicing/silence project and scope reference. Not used, copied or installed; no code is redistributed. |
| [FFmpeg](https://ffmpeg.org/legal.html) | LGPL-2.1-or-later base; GPL-enabled builds differ | `silencedetect` interval concept reference only. This command neither needs nor executes FFmpeg/ffprobe; no source or executable is bundled. |
| [pysubs2](https://github.com/tkarabela/pysubs2) | MIT | Used only by the test suite as an independent SRT parser to verify the emitted `merged.srt` cue count, timing and text (originally validated against pysubs2 1.8.0). `mediawrench chunks` itself does not import it. |

## mediawrench loudness

Original MIT implementation (`src/mediawrench/loudness/`), synthetic waveform
generator (`examples/loudness/synthetic.py`), tests and documentation. The MIT
license does not relicense external executables. No copyrighted audio is included;
examples and tests generate new mathematical waveforms offline.

### External FFmpeg / ffprobe (run as subprocesses, never bundled)

- Upstream: https://github.com/FFmpeg/FFmpeg ; https://ffmpeg.org/legal.html. The
  original FFmpeg authors retain their copyrights. Default FFmpeg licensing is
  LGPL-2.1-or-later; enabling GPL components changes the resulting build's license.
- mediawrench does not copy FFmpeg source, link its libraries, or ship its binaries
  in Git, wheels or source distributions. It invokes executables with argv via
  subprocess. Users must check their chosen build's license.
- The tool was originally validated with John Van Sickle's FFmpeg 7.0.2 static
  Linux x86_64 release (https://johnvansickle.com/ffmpeg/). That binary reports
  `--enable-gpl --enable-version3`; `ffmpeg -L` states GPL version 3 or any later
  version, and the distributor labels all its builds GPLv3. Recorded pins of that
  validation: a 41,888,096-byte archive whose published MD5 was
  `7fa72b652e19bf84c9461e332ea1cdf3` (MD5 is not a secure signature), archive
  SHA-256 `abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67`,
  `ffmpeg` SHA-256 `e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99` and
  `ffprobe` SHA-256 `4f231a1960d83e403d08f7971e271707bec278a9ae18e21b8b5b03186668450d`. The distributor
  provides build information and a source archive link on its download page; see
  the GPL obligations before redistributing such a binary. mediawrench performs no
  redistribution and never downloads FFmpeg.
- loudnorm option semantics are referenced from
  https://ffmpeg.org/ffmpeg-filters.html#loudnorm. No filter implementation was
  copied.

### Reference competitor (no code reused)

https://github.com/slhck/ffmpeg-normalize is MIT, copyright Werner Robitza and
contributors. It already provides measured two-pass processing, statistics and
before/after summaries. It is a research reference, not a dependency, and no
competitor source was copied. This command's scope is a narrow independent
final-encoded-WAV gate with atomic no-overwrite publication; no claim is made that
other tools lack it.

### Build tooling of the original standalone tool

No third-party Python runtime packages are required. The standalone tool locked
pip 25.2 (MIT), setuptools 80.9.0 (MIT) and wheel 0.45.1 (MIT) for its local build
environment only; they were never embedded in its wheel and are not part of
mediawrench. CPython's standard library is used under the PSF License.

## mediawrench postflight

Original MIT implementation (`src/mediawrench/postflight/`, tests, synthetic
fixture generator `examples/postflight/synthetic.py` and
`benchmarks/postflight/`). No competitor or FFmpeg source code was copied, and no
FFmpeg executable is bundled in the wheel or sdist.

### External FFmpeg / ffprobe (run as subprocesses, never bundled)

- Upstream: https://github.com/FFmpeg/FFmpeg and https://ffmpeg.org/legal.html.
  The original FFmpeg authors retain their copyrights.
- Relationship: subprocess argv plus standard ffprobe JSON and `framehash`
  interchange only; no linking.
- Upstream baseline license: LGPL-2.1-or-later; optional components and build flags
  (for example `--enable-gpl`, `--enable-version3`) may make a build GPL. Check the
  license of the actual binary you install, not just the project name.
- The tool was originally validated with John Van Sickle's Linux x86_64 static
  release 7.0.2 (https://johnvansickle.com/ffmpeg/, build information at
  https://johnvansickle.com/ffmpeg/release-readme.txt and sources at
  https://johnvansickle.com/ffmpeg/release-source/). That build reports
  `--enable-gpl --enable-version3` and is distributed under GPLv3. Recorded pins
  of that validation: archive `ffmpeg-release-amd64-static.tar.xz`, 41,888,096
  bytes, SHA-256 `abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67`
  (published MD5 `7fa72b652e19bf84c9461e332ea1cdf3`); extracted `ffmpeg` SHA-256
  `e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99`, `ffprobe`
  SHA-256 `4f231a1960d83e403d08f7971e271707bec278a9ae18e21b8b5b03186668450d`.
  mediawrench does not download or redistribute that or any other build; anyone
  redistributing an FFmpeg binary must assess its corresponding-source obligations.
- Default execution is offline and invokes no VMAF or other model.

### Build tooling of the original standalone tool

The standalone tool pinned only build/setup packages, never runtime dependencies:
pip 26.0.1 (MIT, https://pypi.org/project/pip/26.0.1/), setuptools 80.9.0 (MIT,
https://pypi.org/project/setuptools/80.9.0/) and wheel 0.45.1 (MIT,
https://pypi.org/project/wheel/0.45.1/). Its optional environment bootstrap
restored unmodified CPython 3.14.4 `ensurepip` files (PSF License, from
https://raw.githubusercontent.com/python/cpython/v3.14.4/) inside a local virtual
environment only. None of these are part of mediawrench.

### References (no code reused)

- https://github.com/slhck/ffmpeg-quality-metrics (MIT): competitor/reference only;
  no import, copied code or runtime dependency.
- https://github.com/HaveAGitGat/Tdarr/issues/1478: user-reported failure scenario
  that motivated the decoded-presence check; no implementation reused.
- https://ffmpeg.org/ffprobe.html and
  https://ffmpeg.org/ffmpeg-formats.html#framehash-1: format/CLI interoperability
  documentation; no documentation text is copied into the implementation.

## mediawrench chapters

Formerly the standalone `chapter-roundtrip` tool. The implementation in
`src/mediawrench/chapters/`, its tests (`tests/chapters/`) and the synthetic examples
(`examples/chapters/`, including the Matroska XML fixtures) are original MIT work.
There are no runtime Python dependencies, vendored implementations, media files or
bundled executables.

| Project | Relationship | License / scope |
| --- | --- | --- |
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | Implements the documented [FFmetadata format](https://ffmpeg.org/ffmpeg-formats.html#Metadata); `verify-media` explicitly calls an installed `ffprobe` for read-only verification using its chapter JSON interface ([n7.1.1 reference](https://github.com/FFmpeg/FFmpeg/blob/n7.1.1/fftools/ffprobe.c#L3507-L3527)); the optional integration tests call `ffmpeg`/`ffprobe` on synthetic audio | Default LGPL-2.1-or-later; enabled GPL components change a binary's license, so check the build you install. No code or binary bundled. |
| [MKVToolNix](https://mkvtoolnix.download/) / Matroska | Bounded XML chapter sidecar subset following the [92.0 DTD interface](https://sources.debian.org/src/mkvtoolnix/92.0-1/examples/matroskachapters.dtd/) and [RFC 9559](https://www.rfc-editor.org/rfc/rfc9559.html#section-5.1.7). Original synthetic XML fixtures; no upstream source, DTD text or binaries bundled. | MKVToolNix is GPL-2.0-or-later; the RFC text keeps its IETF terms. No code copied or linked. |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Reads locally saved `info.json`-shaped `duration`/`chapters` data only | Unlicense; no yt-dlp code, downloader, network access or binary bundled. |
| [LosslessCut](https://github.com/mifi/lossless-cut) | Format interoperability context only; no LosslessCut project parser | GPL-2.0 (or-later applicability unverified); no code copied. |
| [ChapterTool](https://github.com/tautcony/ChapterTool) | Existing broad chapter converter; context only | GPLv3+ (per the original research); no source copied or linked. |

## mediawrench drift

Formerly the standalone MIT project drift-review (distribution name
`chronosync-drift-review`), a local review and rendering adapter for ChronoSync. Its
orchestration code (`src/mediawrench/drift/` except `_chronosync/`), tests
(`tests/drift/`), the demo generator (`examples/drift/make_demo.py`) and
documentation are original MIT work. It is not a new correlation, regression or
resampling engine: the DSP comes from the projects below.

### Vendored code: ChronoSync (MIT)

[ChronoSync](https://github.com/Eureka175/ChronoSync), MIT, copyright (c) 2025
ChronoSync contributors, source commit `120002ab887bac3d54222d7375edc1f8aedb07a2`
(package version `0.1.0b0`, release `v0.1.0-beta`). The standalone tool installed it
as a Git dependency pinned to that commit. ChronoSync is not published on PyPI, so
mediawrench ships the four modules the command calls, unmodified, in
`src/mediawrench/drift/_chronosync/`: `fine/gcc_phat.py` (GCC-PHAT delay estimation
with peak, polarity and confidence diagnostics), `fine/peak.py`, `fine/subsample.py`
(parabolic sub-sample refinement) and `drift/regression.py` (`weighted_linear_fit`).
Only the package path changed (the files sit side by side and keep their relative
imports). This is the only third-party source code in mediawrench. Full license text:
[licenses/chronosync-LICENSE.txt](licenses/chronosync-LICENSE.txt):

> MIT License
>
> Copyright (c) 2025 ChronoSync contributors
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

### Runtime dependencies (`drift` extra, installed separately, never vendored)

Versions are the ones the standalone tool locked and validated against.

- [NumPy](https://numpy.org/) 2.4.6, BSD-3-Clause: arrays and numerical operations.
- [SciPy](https://scipy.org/) 1.17.1, BSD-3-Clause: `scipy.fft` and
  `scipy.signal.find_peaks`, used by the vendored ChronoSync modules.
- [python-soxr](https://github.com/dofuuz/python-soxr) 1.1.0, LGPL-2.1-or-later, a
  wrapper around [libsoxr](https://sourceforge.net/projects/soxr/)
  (LGPL-2.1-or-later): constant-ratio VHQ streaming resampling at a floating input
  rate. See the installed wheel's notices.
- [SoundFile](https://github.com/bastibe/python-soundfile) 0.14.0, BSD-3-Clause, a
  wrapper around [libsndfile](https://libsndfile.github.io/libsndfile/)
  (LGPL-2.1-or-later): streamed WAV/RF64 output and clip export. See the installed
  wheel's notices.

The LGPL libraries are used as installed, dynamically loaded packages; mediawrench
does not copy, modify or redistribute them.

### External FFmpeg / ffprobe (run as subprocesses, never bundled)

Probing and decoding every input format go through the `ffmpeg`/`ffprobe` you
install; the license depends on your build (see https://ffmpeg.org/legal.html). No
FFmpeg code or binary is redistributed.

### Test and example audio

The standalone tool's tests used two unmodified speech recordings from the BBC's
[audio-offset-finder](https://github.com/bbc/audio-offset-finder) test suite (source
commit `6b8b12b04104e66d890f8feea080889aa775d9b7`): `timbl_1.mp3`, a Tim
Berners-Lee interview from the BBC Today programme, 9 July 2008 (BBC / Tim
Berners-Lee, [CC BY 3.0 Unported](https://creativecommons.org/licenses/by/3.0/),
[Wikimedia source](https://commons.wikimedia.org/wiki/File:Tim_Berners-Lee_-_Today_(ffmpeg_FLAC_in_OGG).oga)),
and `r4_excerpt2.ogg`, an excerpt of the spoken Wikipedia "BBC Radio 4" article
recorded by Tom Morris ([CC BY-SA 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/),
[Wikimedia source](https://commons.wikimedia.org/wiki/File:BBC_Radio_4.ogg)).
mediawrench does **not** include them. Its tests and demo generate deterministic
speech-like audio at run time with FFmpeg's `anoisesrc` (seeded pink noise gated by
an irregular envelope) and derive every variant from it with FFmpeg filters, so no
recorded media is committed.

### Related projects (no code reused)

The standalone tool's survey compared
[double-ender-sync](https://github.com/ogra/double-ender-sync) 0.2.7,
[Align](https://github.com/FedyaLight/align),
[alignaudio](https://github.com/norihiro/alignaudio),
[audio-offset-finder](https://github.com/bbc/audio-offset-finder) 0.5.5,
[Audalign](https://github.com/benfmiller/audalign) 1.3.1,
[librosa](https://github.com/librosa/librosa) and
[libltc](https://github.com/x42/libltc). They already cover automatic alignment,
drift correction or timecode in broader workflows; this command adds only a
bounded-memory, manual-approval review ledger and streamed render. No source from
them is copied or linked. The test runner is
[pytest](https://github.com/pytest-dev/pytest) (MIT).

## mediawrench cull

Formerly the standalone MIT project cullmerge (Copyright (c) 2026 tianhaogutum). The
implementation in `src/mediawrench/cull/`, its tests (`tests/cull/`), documentation and the three
synthetic example sidecars (`examples/cull/`, generated with ExifTool 13.63 from authored metadata
and an illustrative Camera Raw exposure value) are original MIT work. No third-party source is
vendored; no photo, RAW or rendered image is included.

### Runtime dependencies (optional `cull` extra)

- [lxml](https://lxml.de/), BSD-3-Clause (originally locked and validated at **6.1.3**). Parses XMP
  packets with entity/DTD loading disabled and serializes the merged sidecar; its binary wheels
  include libxml2 and libxslt (MIT) and other libraries under their own notices.
- [simplejson](https://github.com/simplejson/simplejson), MIT OR AFL-2.1 (originally locked and
  validated at **3.20.2**). Writes exact `Decimal` ratings as JSON numbers in plans and audits.

The original project's build backend was hatchling (MIT); development tools were pytest (MIT),
hypothesis (MPL-2.0), ruff (MIT) and build (MIT). Nothing from them is copied.

### Test fixtures (downloaded on demand, never redistributed)

`tests/cull/fixtures/SOURCES.md` records origins and SHA-256 hashes. The optional real-format tests
use, when a user downloads them into the ignored `tests/cull/fixtures/downloaded/`:

- two sidecars from [darktable-tests](https://github.com/darktable-org/darktable-tests) at commit
  `0d3e1ac9acfbdb2c15491fd631b7f48991fcfdc9` (no top-level license; the XMP-v4 file carries a
  CC-BY-SA rights statement and publisher Pascal Obry in its metadata, preserved unchanged);
- two mixed-application sidecars from [digiKam](https://invent.kde.org/graphics/digikam)'s metadata
  tests at commit `33d0457e20adda97c003f3dee652a1749406ff9f` (rights text inside the packets preserved);
- nine `t/images/XMP*.xmp` files from [ExifTool](https://github.com/exiftool/exiftool) 13.63 at commit
  `1da714924f67763b6a8a7013dd2aa1983a16f87b` (ExifTool is distributed under the same terms as Perl).

`tests/cull/fixtures/exiftool-darktable.config` is an authored ExifTool configuration (MIT).

### Format interoperability and related projects (no code reused)

| Project | Relationship | License / scope |
| --- | --- | --- |
| [ExifTool](https://exiftool.org/) | Optional independent writer/reader in tests; documented as a checker. Not used to write the merge because a selective rewrite drops empty unknown RDF lists and reformats the packet. | Perl terms (Artistic or GPL); never bundled or run by the command. |
| [darktable](https://www.darktable.org/) | Reads/writes `darktable:colorlabels`, `xmp:Rating`/`xmp:Label`; its import-default rating policy is followed. Not run. | GPL-3.0-or-later; no code copied. |
| [digiKam](https://www.digikam.org/) | `digiKam:PickLabel` / `digiKam:ColorLabel` value ranges taken from its documented enums. Not run. | GPL-2.0-or-later; no code copied. |
| [Adobe XMP](https://developer.adobe.com/xmp/docs/) | XMP Basic `xmp:Rating` (Real, -1 or [0, 5], missing = unrated), `xmpDM:pick`, packet header hints. | Specification; text not copied. |
| LightCraft, Facet, shoots, Python XMP Toolkit | Surveyed related tools (single-writer XMP preservation, newest-wins sidecar sync, AI rating, XMP bindings); none performs an ancestor-based per-field merge. | Context only; nothing installed, copied or linked. |
