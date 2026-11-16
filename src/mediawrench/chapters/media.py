"""Explicit, read-only local ffprobe verification against conversion reports.

Contract: FFmpeg n7.1.1 fftools/ffprobe.c show_chapters: integer start/end,
positive rational time_base, and optional tags.title. Never use rounded *_time
strings or infer missing endpoints. No executable is downloaded or run on import.
"""
from decimal import Decimal
from fractions import Fraction
import os
from pathlib import Path
import re
import shutil
import subprocess

from .core import ChapterError, FORMATS, MAX_TIME, MICROS, _safe_string, read_json, restore, seconds_us


def _number(value):
    """JSON-safe exact microseconds; noninteger rationals retain their ratio."""
    return value.numerator if value.denominator == 1 else str(value)


def _expected(report):
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1 or report.get("target_format") not in FORMATS):
        raise ChapterError("expected a version 1 conversion report with target_format")
    restore(report)  # Validate the original source as well as the exported projection.
    try:
        doc = restore({"schema_version": report["schema_version"],
                       "source": report["comparison"]["right"]})
    except (KeyError, TypeError) as exc:
        raise ChapterError("conversion report requires comparison.right exported chapters") from exc
    return doc


def _chapters(data):
    if not isinstance(data, dict) or not isinstance(data.get("chapters"), list):
        raise ChapterError("ffprobe JSON requires a chapters array")
    result = []
    for i, chapter in enumerate(data["chapters"]):
        path = f"/chapters/{i}"
        if not isinstance(chapter, dict):
            raise ChapterError(f"ffprobe {path} must be an object")
        base = chapter.get("time_base")
        if not isinstance(base, str) or not re.fullmatch(r"[0-9]+/[0-9]+", base):
            raise ChapterError(f"ffprobe {path}/time_base must be a positive rational")
        try:
            base = Fraction(base)
        except (ValueError, ZeroDivisionError) as exc:
            raise ChapterError(f"ffprobe {path}/time_base must be a positive rational") from exc
        if base <= 0:
            raise ChapterError(f"ffprobe {path}/time_base must be a positive rational")
        tags = chapter.get("tags", {})
        if not isinstance(tags, dict):
            raise ChapterError(f"ffprobe {path}/tags must be an object")
        title = tags.get("title", "")
        _safe_string(title, f"ffprobe {path}/tags/title")
        row = {"title": title}
        for name in ("start", "end"):
            tick = chapter.get(name)
            if type(tick) is not int:
                raise ChapterError(f"ffprobe {path}/{name} must be an integer tick")
            endpoint = tick * base * MICROS
            if endpoint > MAX_TIME:
                raise ChapterError(f"ffprobe {path}/{name} must not exceed {MAX_TIME} microseconds")
            row[name + "_us"] = endpoint
        result.append(row)
    return result


def _duration_value(value, path):
    if value is None or value == "N/A":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ChapterError(f"ffprobe {path} must be nonnegative decimal seconds or N/A")
    try:
        duration = Fraction(value) * MICROS
    except (ValueError, OverflowError) as exc:
        raise ChapterError(f"ffprobe {path} contains an invalid duration") from exc
    return _duration_range(duration, path)


def _duration_range(value, path):
    if not 0 <= value <= MAX_TIME:
        raise ChapterError(f"ffprobe {path} duration must be in [0, {MAX_TIME}] microseconds")
    return value


def _probe_duration(data):
    """Prefer container duration; otherwise longest available A/V stream length.

    These are demuxer/header estimates, even when ticks give exact arithmetic.
    A chapter end or a DURATION tag is never a duration source.
    """
    format_data = data.get("format", {})
    if not isinstance(format_data, dict):
        raise ChapterError("ffprobe /format must be an object")
    duration = _duration_value(format_data.get("duration"), "/format/duration")
    if duration is not None:
        return duration, "/format/duration", []
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise ChapterError("ffprobe /streams must be an array")
    candidates, unavailable = [], []
    for i, stream in enumerate(streams):
        path = f"/streams/{i}"
        if not isinstance(stream, dict):
            raise ChapterError(f"ffprobe {path} must be an object")
        disposition = stream.get("disposition", {})
        if not isinstance(disposition, dict):
            raise ChapterError(f"ffprobe {path}/disposition must be an object")
        if stream.get("codec_type") not in ("audio", "video") or disposition.get("attached_pic") == 1:
            continue
        tick = stream.get("duration_ts")
        if tick is not None and tick != "N/A":
            base = stream.get("time_base")
            if (type(tick) is not int or not isinstance(base, str)
                    or not re.fullmatch(r"[0-9]+/[0-9]+", base)):
                raise ChapterError(f"ffprobe {path} requires integer duration_ts and positive time_base")
            try:
                base = Fraction(base)
            except (ValueError, ZeroDivisionError) as exc:
                raise ChapterError(f"ffprobe {path}/time_base must be a positive rational") from exc
            if base <= 0:
                raise ChapterError(f"ffprobe {path}/time_base must be a positive rational")
            source = path + "/duration_ts"
            duration = _duration_range(tick * base * MICROS, source)
        else:
            source = path + "/duration"
            duration = _duration_value(stream.get("duration"), source)
        if duration is None:
            unavailable.append(path)
        else:
            candidates.append((duration, source))
    if not candidates:
        return None, None, []
    diagnostics = [{"kind": "stream_duration_fallback",
                    "reason": "format duration unavailable; using longest available audio/video stream length"}]
    if unavailable:
        diagnostics.append({"kind": "incomplete_stream_durations", "paths": unavailable,
                            "reason": "some audio/video stream durations are unavailable"})
    duration, source = max(candidates, key=lambda item: item[0])
    return duration, source, diagnostics


def _unavailable_duration(expected, expected_source, reason):
    return {"status": "unavailable", "expected_us": expected, "actual_us": None,
            "expected_source": expected_source,
            "source": None, "estimated": None, "delta_us": None, "within_tolerance": None,
            "diagnostics": [{"kind": "unavailable_duration", "reason": reason}]}


def verify_media(media, report, *, ffprobe, tolerance_us=1000, timeout=15, verify_duration=False,
                 expected_duration=None):
    """Probe an existing local file and check exported titles/order/endpoints.

    Return status passed/different/skipped; equal is None for skipped. Invalid
    reports, malformed probe output and execution failures raise ChapterError.
    Actual endpoints above MAX_TIME microseconds are malformed regardless of
    tolerance; in-range rational endpoints remain exact, without rounding.
    Overlapping adjacent intervals in probe order differ regardless of tolerance;
    touching half-open boundaries and gaps are allowed.
    tolerance_us is an inclusive integer bound; timeout is 1..300 seconds.
    verify_duration additionally compares the exported boundary with ffprobe's
    reported format/stream duration estimate. Unavailable duration skips that
    verification (equal=None), unless known chapter differences already fail.
    expected_duration overrides only that expectation, requires verify_duration,
    and accepts exact decimal seconds (digit string, int or Decimal), never
    rounding. It must cover the exported chapters, but may differ from the
    report's duration boundary. expected_source records the selection.
    """
    if type(tolerance_us) is not int or not 0 <= tolerance_us <= MAX_TIME:
        raise ChapterError(f"tolerance_us must be an integer in [0, {MAX_TIME}]")
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ChapterError("ffprobe timeout must be an integer in [1, 300] seconds")
    if type(verify_duration) is not bool:
        raise ChapterError("verify_duration must be a boolean")
    if expected_duration is not None and not verify_duration:
        raise ChapterError("expected_duration requires --verify-duration (verify_duration=True)")
    doc = _expected(report)
    duration_expected = doc.duration_us
    expected_source = "conversion report comparison.right.duration_us"
    if expected_duration is not None:
        if (isinstance(expected_duration, bool)
                or not isinstance(expected_duration, (str, int, Decimal))
                or (isinstance(expected_duration, str)
                    and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", expected_duration))):
            raise ChapterError("expected_duration must be nonnegative exact decimal seconds")
        duration_expected = seconds_us(expected_duration, "expected_duration")
        if duration_expected < doc.chapters[-1].end_us:
            raise ChapterError("expected_duration must not precede the exported chapter extent")
        expected_source = "explicit expected_duration"
    expected = [{"title": c.title, "start_us": c.start_us, "end_us": c.end_us}
                for c in doc.chapters]
    path = Path(media).resolve()
    if not path.is_file():
        raise ChapterError(f"media must be an existing local file: {path}")
    executable = shutil.which(str(ffprobe))
    result = {"schema_version": 1, "status": "skipped", "equal": None,
              "media": str(path), "expectation": "conversion report comparison.right",
              "scope": "chapter titles, order, starts and ends only",
              "tolerance_us": tolerance_us, "expected": expected,
              "actual": None, "checks": [], "differences": []}
    if verify_duration:
        result["scope"] = "chapter titles, order, starts, ends and reported media duration estimate"
        result["duration"] = _unavailable_duration(duration_expected, expected_source, "ffprobe has not run")
    if executable is None:
        result["reason"] = f"ffprobe executable unavailable: {ffprobe}; real media verification skipped"
        if verify_duration:
            result["duration"] = _unavailable_duration(duration_expected, expected_source, result["reason"])
        return result
    # Absolute local input cannot be interpreted as an option or URL. Restrict
    # protocols too, so local playlists cannot trigger remote fetches.
    command = [executable, "-v", "error", "-threads", "1", "-protocol_whitelist", "file",
               "-show_chapters"]
    if verify_duration:
        command += ["-show_format", "-show_streams"]
    command += ["-of", "json", str(path)]
    try:
        probe = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", timeout=timeout,
                               env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
    except subprocess.TimeoutExpired as exc:
        raise ChapterError(f"ffprobe timed out after {timeout} seconds") from exc
    except (OSError, UnicodeError) as exc:
        raise ChapterError(f"ffprobe failed: {exc}") from exc
    if probe.returncode:
        raise ChapterError(f"ffprobe exited {probe.returncode}: {probe.stderr.strip()}")
    data = read_json(probe.stdout)
    actual = _chapters(data)
    result["probe"] = {"command": command, "returncode": probe.returncode}
    result["actual"] = [{k: _number(v) if isinstance(v, Fraction) else v for k, v in c.items()}
                        for c in actual]
    differences = result["differences"]
    if len(expected) != len(actual):
        differences.append({"path": "/chapters", "kind": "length",
                            "expected": len(expected), "actual": len(actual)})
    for i in range(max(len(expected), len(actual))):
        path = f"/chapters/{i}"
        if i >= len(actual):
            differences.append({"path": path, "kind": "missing", "expected": expected[i], "actual": None})
        elif i >= len(expected):
            differences.append({"path": path, "kind": "unexpected", "expected": None,
                                "actual": result["actual"][i]})
        else:
            wanted, found = expected[i], actual[i]
            check = {"index": i, "title": {"expected": wanted["title"], "actual": found["title"],
                                             "equal": wanted["title"] == found["title"]}}
            if not check["title"]["equal"]:
                differences.append({"path": path + "/title", "kind": "changed",
                                    "expected": wanted["title"], "actual": found["title"]})
            for name in ("start_us", "end_us"):
                delta = found[name] - wanted[name]
                check[name] = {"expected_us": wanted[name], "actual_us": _number(found[name]),
                               "delta_us": _number(delta), "within_tolerance": abs(delta) <= tolerance_us}
                if not check[name]["within_tolerance"]:
                    differences.append({"path": path + "/" + name, "kind": "outside_tolerance",
                                        **check[name], "tolerance_us": tolerance_us})
            result["checks"].append(check)
    # Structural order/interval/overlap checks apply even to unexpected chapters and
    # remain failures with a generous tolerance. Nothing is sorted or inferred.
    for i, chapter in enumerate(actual):
        if i and chapter["start_us"] <= actual[i - 1]["start_us"]:
            differences.append({"path": f"/chapters/{i}/start_us", "kind": "order",
                                "reason": "starts must be strictly increasing in probe order"})
        if chapter["end_us"] <= chapter["start_us"] or chapter["start_us"] < 0:
            differences.append({"path": f"/chapters/{i}", "kind": "invalid_interval",
                                "reason": "requires 0 <= start < end"})
        if i:
            previous = actual[i - 1]
            overlap = (min(previous["end_us"], chapter["end_us"])
                       - max(previous["start_us"], chapter["start_us"]))
            if overlap > 0:
                differences.append({"path": f"/chapters/{i}/start_us", "kind": "overlap",
                                    "previous_path": f"/chapters/{i - 1}/end_us",
                                    "previous_end_us": _number(previous["end_us"]),
                                    "actual_us": _number(chapter["start_us"]),
                                    "overlap_us": _number(overlap)})
    result.update(status="different" if differences else "passed", equal=not differences)
    if verify_duration:
        duration, source, diagnostics = _probe_duration(data)
        if duration is None:
            reason = "ffprobe format and audio/video stream durations unavailable; duration verification skipped"
            result["duration"] = _unavailable_duration(duration_expected, expected_source, reason)
            if not differences:
                result.update(status="skipped", equal=None, reason=reason)
        else:
            delta = duration - duration_expected
            within = abs(delta) <= tolerance_us
            result["duration"] = {"status": "passed" if within else "different",
                                  "expected_us": duration_expected, "actual_us": _number(duration),
                                  "expected_source": expected_source,
                                  "source": source, "estimated": True, "delta_us": _number(delta),
                                  "within_tolerance": within,
                                  "diagnostics": diagnostics + [{"kind": "estimated_duration",
                                      "reason": "ffprobe reported duration is a demuxer/header estimate; "
                                                "no full decode or content-integrity verification performed"}]}
            if not within:
                differences.append({"path": "/duration_us", "kind": "outside_tolerance",
                                    "expected_us": duration_expected, "actual_us": _number(duration),
                                    "delta_us": _number(delta), "tolerance_us": tolerance_us})
                result.update(status="different", equal=False)
    return result
