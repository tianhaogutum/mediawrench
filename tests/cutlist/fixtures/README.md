These two synthetic, silent black 16x16 MPEG-4 video fixtures were generated
locally for origin validation tests. They contain 60 frames at 24 fps, with
selected video stream index 0 start_time 0 and 1 second respectively. No external
media was used; these project-created fixtures are covered by the root MIT
license. They were generated with an FFmpeg 7.0 static build, whose ffprobe confirmed
both rates and start times. Neither executable is bundled. Tests do not regenerate
fixtures; real tests read them using an explicit ffprobe.

Regenerate from the repository root with your ffmpeg executable:

```bash
/path/to/ffmpeg -nostdin -v error -f lavfi -i color=c=black:s=16x16:r=24:d=2.5 \
  -an -c:v mpeg4 -bf 0 -q:v 10 -output_ts_offset 0 -fflags +bitexact -flags:v +bitexact \
  tests/cutlist/fixtures/origin-zero.mp4
/path/to/ffmpeg -nostdin -v error -f lavfi -i color=c=black:s=16x16:r=24:d=2.5 \
  -an -c:v mpeg4 -bf 0 -q:v 10 -output_ts_offset 1 -fflags +bitexact -flags:v +bitexact \
  tests/cutlist/fixtures/origin-one.mp4
```

Existing files cause ffmpeg to refuse overwrite. Byte output may differ with
another ffmpeg build; tests assert metadata/behavior rather than these hashes.

`origin-multistream.mp4` remuxes those videos without decoding and adds locally
generated silence (mono 8000 Hz ALAC). It is 4009 bytes, SHA-256
`7c8f51609e4379f52b809337f9765c35fa00e1dad3f60bebd2a19e113c70a33a`.
The same project MIT license applies. Regenerate the two inputs above first:

```bash
/path/to/ffmpeg -nostdin -v error -copyts \
  -f lavfi -i anullsrc=r=8000:cl=mono:d=2.5 \
  -i tests/cutlist/fixtures/origin-one.mp4 -i tests/cutlist/fixtures/origin-zero.mp4 \
  -map 0:a:0 -map 1:v:0 -map 2:v:0 -c:a alac -c:v copy \
  -fflags +bitexact -flags:a +bitexact -avoid_negative_ts disabled \
  tests/cutlist/fixtures/origin-multistream.mp4
```

Its absolute stream indices deliberately differ from video ordinals:

| Absolute index | Type | avg / r frame rate | start_time seconds |
| --- | --- | --- | --- |
| 0 | audio | `0/0` / `0/0` | `0.000000` |
| 1 | video | `24/1` / `24/1` | `1.000000` |
| 2 | video | `24/1` / `24/1` | `0.000000` |

Index 3 is absent. Both videos retain 60 frames; container duration is 3.5
seconds because video 1 ends later. Duration always comes from the container.
The 2.5-second plans used for the single-video fixtures therefore do not apply;
use `{"keep":[1],"frameRate":"24"}` without declared duration here.

Independently inspect each index with the supported origin query (repeat with
`-select_streams 1`, `2`, and `3`; index 3 yields an empty streams array):

```bash
/path/to/ffprobe -v error -protocol_whitelist file -select_streams 0 \
  -show_program_version \
  -show_entries format=duration,format_name,size:program_version=version:stream=index,codec_type,avg_frame_rate,r_frame_rate,start_time \
  -of json -i tests/cutlist/fixtures/origin-multistream.mp4
```

[origin-multistream.json](origin-multistream.json) retains generation argv, input,
fixture and executable hashes, tool versions and expected metadata. Only ffprobe
is needed for tests; no tests decode media or regenerate this fixture.


`rates-multistream.mp4` contains competing rates with zero origins, generated
entirely from local silence and black 16x16 lavfi video (root MIT license).
It contains 60 frames at 24/1 and 75 frames at 30000/1001, with no B frames,
plus mono 8000 Hz ALAC audio. Generate it into a fresh path:

```bash
/path/to/ffmpeg -nostdin -v error \
  -f lavfi -i anullsrc=r=8000:cl=mono:d=2.5 \
  -f lavfi -i color=c=black:s=16x16:r=24:d=2.5 \
  -f lavfi -i color=c=black:s=16x16:r=30000/1001:d=2.5 \
  -map 0:a:0 -map 1:v:0 -map 2:v:0 -c:a alac -c:v mpeg4 \
  -bf 0 -q:v 10 -threads:v 1 -fflags +bitexact -flags:a +bitexact -flags:v +bitexact \
  tests/cutlist/fixtures/rates-multistream.mp4
```

| Absolute index | Type | avg / r frame rate | start_time seconds |
| --- | --- | --- | --- |
| 0 | audio | `0/0` / `0/0` | `0.000000` |
| 1 | video | `24/1` / `24/1` | `0.000000` |
| 2 | video | `30000/1001` / `30000/1001` | `0.000000` |

The supported exact-index query above applies with `-i tests/cutlist/fixtures/rates-multistream.mp4`
and each of `-select_streams 0`, `1`, `2`. Container duration is exactly
`2.502500` seconds; the existing conservative duration policy floors it to
2502 ms. Plans omit duration. This metadata check makes no decoded-timing claim.

[rates-multistream.json](rates-multistream.json) retains generation argv,
executable versions/hashes and independent supported-query responses for each
index. The fixture is 4732 bytes, SHA-256
`5cd52bb37864055f04fb85b41cbf69b8ac53566e91ac4011106105131db5b240`.
A second generation with the same FFmpeg build was byte-identical. Other builds may
produce different bytes; real tests require only ffprobe and assert semantic
metadata and behavior.
