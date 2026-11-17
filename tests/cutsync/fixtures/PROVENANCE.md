# Fixture provenance

`losslesscut/test1.csv`, `test2.csv`, `test3.csv`, and `sample.srt` were downloaded
unchanged on 2026-11-11 from
https://github.com/mifi/lossless-cut/tree/4c7ab1cb0c9acaefabb48e997bc909562dc3624d/src/renderer/src/test/fixtures.
They exercise actual upstream parser test data, including CRLF SRT, comma and
dot decimal separators, fractional seconds, and historical CSV without a header.
`losslesscut/LICENSE` is that repository's GPL-2.0 license; those fixtures retain
their upstream license. The `mediawrench cutsync` source is MIT licensed.

`editor-export.ass` was authored for the original standalone `srt-cut-sync`
project (MIT). `ffmpeg-export.srt` is its actual conversion using FFmpeg 9.0.2:

```sh
ffmpeg -hide_banner -loglevel error -nostdin -i tests/cutsync/fixtures/editor-export.ass \
  -c:s srt tests/cutsync/fixtures/ffmpeg-export.srt
```

The examples in `examples/cutsync/` are authored dialogue with nonconsecutive
original cue numbers, markup, multilingual text and multiline captions. Native `.llc` examples follow
the schema and serializer read from the pinned upstream source, but were not
saved through the LosslessCut GUI.
