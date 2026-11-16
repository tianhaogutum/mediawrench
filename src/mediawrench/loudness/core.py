"""Original implementation; FFmpeg is invoked as an external executable only."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
from threading import Event
import time
from typing import Any

MIN_SECONDS = 3.0
OUTPUT_RATE = 48000
DURATION_TOLERANCE = 0.001
INPUT_DEMUXERS = "wav,flac,matroska"
PCM_CODECS = {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_u8", "pcm_f32le", "pcm_f64le"}


class AudioError(Exception):
    """An expected processing failure with a reportable category."""

    def __init__(self, message: str, status: str = "ERROR"):
        super().__init__(message)
        self.status = status


class DeadlineExceeded(AudioError):
    """The shared monotonic job budget has expired."""


class JobCancelled(AudioError):
    """The caller requested cooperative cancellation."""


class Job:
    """Cooperative file-I/O checkpoints and one budget for every subprocess."""

    def __init__(self, total_timeout: float):
        self.started = time.monotonic_ns()
        self.deadline = self.started + round(total_timeout * 1_000_000_000)
        self.cancel_event: Event | None = None
        self.stage = "preparation"
        self.boundary = 0
        self.stages = dict.fromkeys(("preparation", "analysis", "encoding", "verification",
                                    "publication", "cleanup"), 0)

    def remaining(self) -> float:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled("Job cancelled")
        remaining = (self.deadline - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise DeadlineExceeded(f"Total job deadline exceeded during {self.stage}")
        return remaining

    def switch(self, stage: str) -> None:
        # Differences of rounded cumulative boundaries preserve an exact integer sum.
        now = round((time.monotonic_ns() - self.started) / 1_000_000)
        self.stages[self.stage] += now - self.boundary
        self.boundary = now
        self.stage = stage

    def timings(self) -> dict[str, Any]:
        self.switch(self.stage)
        return {"unit": "milliseconds", "stages": dict(self.stages), "total": self.boundary,
                "scope": "Through audio publication and staging cleanup; excludes final report delivery"}


@dataclass(frozen=True)
class Profile:
    target_i: float
    target_tp: float
    tolerance: float
    target_lra: float = 11.0

    def validate(self) -> None:
        bounds = {"target_i": (-70, -5), "target_tp": (-9, 0),
                  "tolerance": (0, 2), "target_lra": (1, 50)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if not math.isfinite(value) or not low <= value <= high:
                raise AudioError(f"{name} must be finite and in [{low}, {high}]")
        if self.tolerance <= 0:
            raise AudioError("tolerance must be positive")
        if self.target_i >= self.target_tp:
            raise AudioError("target_i must be below the true-peak ceiling")

    def filter(self) -> str:
        return (f"loudnorm=I={self.target_i}:TP={self.target_tp}:"
                f"LRA={self.target_lra}")


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout: float = 120.0
    total_timeout: float = 600.0

    def validate(self) -> None:
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 3600:
            raise AudioError("timeout must be finite, positive and <=3600 seconds")
        if not math.isfinite(self.total_timeout) or not 0 < self.total_timeout <= 86400:
            raise AudioError("total_timeout must be finite, positive and <=86400 seconds")


class Runner:
    def __init__(self, tools: Toolchain, job: Job | None = None):
        tools.validate()
        self.tools = tools
        self.job = job or Job(tools.total_timeout)
        self.commands: list[list[str]] = []

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        remaining = self.job.remaining()
        self.commands.append(argv)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
        except FileNotFoundError as exc:
            raise AudioError(f"Executable not found: {argv[0]}") from exc
        except OSError as exc:
            raise AudioError(f"Cannot execute {argv[0]}: {exc}") from exc
        try:
            process_deadline = time.monotonic() + self.tools.timeout
            while True:
                remaining = self.job.remaining()
                process_remaining = process_deadline - time.monotonic()
                if process_remaining <= 0:
                    raise AudioError(f"Subprocess timed out after {self.tools.timeout}s: {argv[0]}")
                try:
                    # communicate drains both pipes on every retry. Neither the process
                    # cap nor the job deadline resets while waiting for cancellation.
                    stdout, stderr = process.communicate(timeout=min(0.1, process_remaining, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            # Also reap descendants of local wrapper executables on POSIX. Interrupts
            # must finish this cleanup before staging files can safely be removed.
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError):
                # macOS reports EPERM instead of ESRCH for a group whose members are all zombies.
                pass
            process.communicate()
            raise
        self.job.remaining()
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if result.returncode:
            raise AudioError(f"{Path(argv[0]).name} exited {result.returncode}: "
                             f"{result.stderr[-8000:]}")
        return result

    def ffmpeg_prefix(self) -> list[str]:
        return [self.tools.ffmpeg, "-hide_banner", "-nostdin", "-nostats",
                "-loglevel", "info", "-xerror", "-threads", "1",
                "-filter_threads", "1", "-filter_complex_threads", "1",
                "-protocol_whitelist", "file", "-format_whitelist", INPUT_DEMUXERS]


def sha256(path: Path, job: Job | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if job:
                job.remaining()
            digest.update(block)
    if job:
        job.remaining()
    return digest.hexdigest()


def _copy_source(source: Path, snapshot: Path, job: Job) -> None:
    with source.open("rb") as src, snapshot.open("xb") as dest:
        while True:
            job.remaining()
            block = src.read(1024 * 1024)
            if not block:
                break
            dest.write(block)
    job.remaining()


def list_streams(source: str | Path, tools: Toolchain | None = None, *,
                 cancel_event: Event | None = None) -> dict[str, Any]:
    """Read a local Matroska inventory with ffprobe only; never stage or publish.

    All stream types/codecs/layouts are listed, without testing decodability or
    normalization eligibility. Missing metadata stays null; layouts are not inferred.
    Expected failures return structured JSON, just like normalize().
    Set and leave the caller-owned Event set until return to cancel cooperatively.
    A pre-set Event skips probing; cancellation reaps any live probe and writes no files.
    """
    source = Path(source).absolute()
    result: dict[str, Any] = {"schema_version": 1, "status": "ERROR", "source": str(source),
                              "published": False, "commands": []}
    try:
        if cancel_event is not None and not isinstance(cancel_event, Event):
            raise AudioError("cancel_event must be a threading.Event or None")
        runner = Runner(tools or Toolchain())
        result["commands"] = runner.commands
        runner.job.cancel_event = cancel_event
        runner.job.remaining()
        if not source.is_file():
            raise AudioError("Input must be an existing local regular file")
        raw = runner.run([runner.tools.ffprobe, "-v", "error", "-threads", "1",
                          "-protocol_whitelist", "file", "-format_whitelist", INPUT_DEMUXERS,
                          "-show_entries", "stream=index,codec_type,codec_name,channels,channel_layout,sample_rate,bits_per_raw_sample:"
                          "stream_tags=language,title:stream_disposition=default,forced:format=format_name",
                          "-of", "json", str(source)])
        try:
            info = json.loads(raw.stdout)
            if not isinstance(info, dict) or not isinstance(info.get("format"), dict):
                raise ValueError("expected format object")
            fmt = info["format"]["format_name"]
            if not isinstance(fmt, str):
                raise ValueError("expected format name")
            if fmt != "matroska,webm":
                raise AudioError("Stream listing requires a local Matroska container", "UNSUPPORTED")
            streams = info["streams"]
            if not isinstance(streams, list):
                raise ValueError("expected streams array")
            inventory = []
            indices = set()
            for stream in streams:
                if not isinstance(stream, dict):
                    raise ValueError("expected stream object")
                index = stream["index"]
                if type(index) is not int or index < 0 or index in indices:
                    raise ValueError("expected unique nonnegative integer stream indices")
                indices.add(index)
                codec_type, codec = stream.get("codec_type"), stream.get("codec_name")
                channels, layout = stream.get("channels"), stream.get("channel_layout")
                if any(value is not None and not isinstance(value, str)
                       for value in (codec_type, codec, layout)):
                    raise ValueError("expected string stream metadata")
                if channels is not None and (type(channels) is not int or channels <= 0):
                    raise ValueError("expected positive integer channel count")
                rate = stream.get("sample_rate")
                if rate not in (None, "", "N/A", "unknown"):
                    if not isinstance(rate, str) or not rate.isascii() or not rate.isdecimal():
                        raise ValueError("expected ASCII decimal sample rate string")
                    rate = int(rate) or None
                else:
                    rate = None
                raw_depth = stream.get("bits_per_raw_sample")
                if raw_depth not in (None, "", "N/A", "unknown"):
                    if not isinstance(raw_depth, str) or not raw_depth.isascii() or not raw_depth.isdecimal():
                        raise ValueError("expected ASCII decimal raw sample bit depth string")
                    raw_depth = int(raw_depth) or None
                else:
                    raw_depth = None
                tags, disposition = stream.get("tags", {}), stream.get("disposition", {})
                if not isinstance(tags, dict) or not isinstance(disposition, dict):
                    raise ValueError("expected tags and disposition objects")
                language, title = tags.get("language"), tags.get("title")
                if any(value is not None and not isinstance(value, str) for value in (language, title)):
                    raise ValueError("expected string language/title metadata")
                flags = {}
                for name in ("default", "forced"):
                    if name in disposition:
                        value = disposition[name]
                        if type(value) is not int or value not in (0, 1):
                            raise ValueError("expected integer 0/1 disposition flags")
                        flags[name] = bool(value)
                    else:
                        flags[name] = None
                inventory.append({"stream_index": index, "codec_type": codec_type, "codec": codec,
                                  "channels": channels, "sample_rate": rate, "bits_per_raw_sample": raw_depth,
                                  "channel_layout": None if layout in (None, "", "unknown") else layout,
                                  "language": language, "title": title, **flags})
            runner.job.remaining()
        except (KeyError, ValueError, TypeError) as exc:
            raise AudioError(f"Invalid/incomplete ffprobe output: {exc}") from exc
        result.update(status="OK", format=fmt,
                      streams=sorted(inventory, key=lambda stream: stream["stream_index"]))
    except (AudioError, OSError, KeyboardInterrupt) as exc:
        result["status"] = exc.status if isinstance(exc, AudioError) else "ERROR"
        result["error"] = "Job cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
        if isinstance(exc, OSError):
            result["errno"] = exc.errno
        if isinstance(exc, DeadlineExceeded):
            result["error_kind"] = "deadline_exceeded"
        elif isinstance(exc, (JobCancelled, KeyboardInterrupt)):
            result["error_kind"] = "cancelled"
    return result


def probe(path: Path, runner: Runner, *, candidate: bool = False,
          audio_stream: int | None = None) -> dict[str, Any]:
    result = runner.run([runner.tools.ffprobe, "-v", "error", "-threads", "1",
                         "-protocol_whitelist", "file", "-format_whitelist", INPUT_DEMUXERS,
                         "-show_streams", "-show_format",
                         "-of", "json", str(path)])
    try:
        info = json.loads(result.stdout)
        streams = info["streams"]
        fmt = info["format"]["format_name"]
        matroska = fmt == "matroska,webm"
        if fmt not in {"wav", "flac", "matroska,webm"}:
            raise AudioError("Only local PCM WAV, FLAC and Matroska input containers are supported",
                             "UNSUPPORTED")
        if candidate or not matroska:
            if len(streams) != 1 or streams[0].get("codec_type") != "audio":
                raise AudioError("Exactly one audio stream and no video/attachments/data streams required",
                                 "UNSUPPORTED")
        if audio_stream is None:
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            if not audio:
                raise AudioError("No audio streams found", "UNSUPPORTED")
            if len(audio) != 1:
                indices = [s["index"] for s in audio]
                raise AudioError(f"Multiple audio streams {indices}; specify --audio-stream INDEX",
                                 "UNSUPPORTED")
            stream = audio[0]
        else:
            matches = [s for s in streams if s["index"] == audio_stream]
            if not matches:
                raise AudioError(f"Stream index {audio_stream} does not exist", "UNSUPPORTED")
            if len(matches) != 1:
                raise AudioError(f"Ambiguous stream index {audio_stream}", "UNSUPPORTED")
            stream = matches[0]
            if stream.get("codec_type") != "audio":
                raise AudioError(f"Stream index {audio_stream} is not an audio stream", "UNSUPPORTED")
        codec = stream["codec_name"]
        if fmt == "wav" and codec not in PCM_CODECS:
            raise AudioError("Unsupported WAV codec; use uncompressed PCM", "UNSUPPORTED")
        if matroska and codec not in PCM_CODECS | {"flac"}:
            raise AudioError("Selected Matroska audio must be PCM or FLAC", "UNSUPPORTED")
        channels = int(stream["channels"])
        if channels not in (1, 2):
            raise AudioError("Only mono or stereo audio is supported", "UNSUPPORTED")
        layout = stream.get("channel_layout")
        inferred_layout = layout in (None, "", "unknown")
        if inferred_layout:
            layout = {1: "mono", 2: "stereo"}[channels]
        if layout != {1: "mono", 2: "stereo"}[channels]:
            raise AudioError("Only mono or stereo channel layouts are supported", "UNSUPPORTED")
        # Matroska's format duration may describe video or another track, and its stream
        # duration tags may be missing or include timestamp offsets. Count decoded samples
        # in a private PCM WAV instead; normalize() fills this value before measurement.
        duration = None if matroska else float(info["format"].get("duration", stream.get("duration", "nan")))
        if not matroska and (not math.isfinite(duration) or duration < MIN_SECONDS):
            raise AudioError(f"Clips shorter than {MIN_SECONDS:g}s or without finite duration are unsupported",
                             "UNSUPPORTED")
        rate = int(stream["sample_rate"])
        if candidate and (fmt != "wav" or codec != "pcm_s24le" or rate != OUTPUT_RATE):
            raise AudioError("Candidate must be 48 kHz PCM 24-bit WAV", "UNSUPPORTED")
        return {"format": fmt, "codec": codec, "channels": channels,
                "sample_rate": rate, "duration_seconds": duration,
                "stream_index": stream["index"], "channel_layout": layout,
                "channel_layout_inferred": inferred_layout}
    except (KeyError, ValueError, TypeError) as exc:
        raise AudioError(f"Invalid/incomplete ffprobe output: {exc}", "UNSUPPORTED") from exc


def parse_loudnorm(stderr: str) -> dict[str, Any]:
    """Locate loudnorm's JSON object without depending on log prefixes."""
    found = []
    decoder = json.JSONDecoder()
    for index, char in enumerate(stderr):
        if char != "{":
            continue
        try:
            item, _ = decoder.raw_decode(stderr[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "input_i" in item:
            found.append(item)
    if len(found) != 1:
        raise AudioError("Expected exactly one loudnorm JSON measurement")
    return found[0]


def finite_measurement(raw: dict[str, Any]) -> dict[str, float]:
    values = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise AudioError(f"Missing or invalid loudnorm measurement: {key}", "UNSUPPORTED") from exc
        if not math.isfinite(value):
            raise AudioError(f"Nonfinite {key} ({raw[key]}); silence or unmeasurable audio is unsupported",
                             "UNSUPPORTED")
        values[key] = value
    return values


def measure(path: Path, profile: Profile, runner: Runner) -> dict[str, Any]:
    result = runner.run(runner.ffmpeg_prefix() + ["-i", str(path), "-map", "0:a:0",
                        "-af", profile.filter() + ":print_format=json", "-threads", "1",
                        "-f", "null", "-"])
    raw = parse_loudnorm(result.stderr)
    finite_measurement(raw)
    return raw


def gate(raw: dict[str, Any], profile: Profile) -> dict[str, Any]:
    values = finite_measurement(raw)
    deviation = abs(values["input_i"] - profile.target_i)
    return {"integrated_lufs": values["input_i"], "true_peak_dbtp": values["input_tp"],
            "lufs_error": round(deviation, 8),
            "lufs_pass": deviation <= profile.tolerance + 1e-9,
            "peak_pass": values["input_tp"] <= profile.target_tp + 1e-9}


def verify_candidate(path: str | Path, profile: Profile,
                     tools: Toolchain | None = None, *,
                     cancel_event: Event | None = None) -> dict[str, Any]:
    """Independently measure an existing candidate; never modify or publish it.

    Returns PASS/FAIL. Unsupported or failed measurements raise AudioError.
    Set and leave a caller-owned Event set until return to cancel cooperatively.
    Cancellation raises JobCancelled after reaping any live probe/measurement;
    a pre-set Event skips path resolution and subprocesses with valid settings.
    Resolved candidates must be regular files; missing paths and symlink loops
    retain OSError (FileNotFoundError and ELOOP respectively).
    """
    if cancel_event is not None and not isinstance(cancel_event, Event):
        raise AudioError("cancel_event must be a threading.Event or None")
    profile.validate()
    runner = Runner(tools or Toolchain())
    runner.job.cancel_event = cancel_event
    runner.job.remaining()
    # Path.resolve translates ELOOP to RuntimeError on Python 3.11/3.12.
    path = Path(os.path.realpath(Path(path), strict=True))
    if not stat.S_ISREG(path.stat().st_mode):
        raise AudioError("Candidate must be an existing local regular file")
    media = probe(path, runner, candidate=True)
    runner.job.remaining()
    raw = measure(path, profile, runner)
    checks = gate(raw, profile)
    runner.job.remaining()
    return {"status": "PASS" if checks["lufs_pass"] and checks["peak_pass"] else "FAIL",
            "media": media, "measurement": raw, "checks": checks, "commands": runner.commands}


def _write_report(path: Path, report: dict[str, Any], job: Job | None = None) -> None:
    """Write complete strict JSON and atomically create a new report, never clobber."""
    if job:
        job.remaining()
    fd, temp_name = tempfile.mkstemp(prefix=".loudnorm-report-", dir=path.parent)
    temp = Path(temp_name)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if job:
                job.remaining()
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if job:
            job.remaining()
        os.link(temp, path)
        if job:
            job.remaining()
        temp.unlink()
        if job:
            job.remaining()
    except BaseException:
        # A signal can arrive inside link(), after the new name already exists.
        if path.exists():
            current = path.stat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        raise
    finally:
        temp.unlink(missing_ok=True)


def _safe_profile(profile: Profile) -> dict[str, Any]:
    # Invalid CLI numbers still need a standards-compliant failure report.
    return {key: value if math.isfinite(value) else str(value)
            for key, value in asdict(profile).items()}


def _resolve_alias(path: Path) -> Path:
    """Resolve even missing destinations, preserving native symlink-loop errors."""
    resolved = Path(os.path.realpath(path))
    try:
        resolved.stat()
    except OSError as exc:
        # Match non-strict Path.resolve, except its RuntimeError translation of
        # ELOOP on Python 3.11/3.12. Windows uses ERROR_CANT_RESOLVE_FILENAME.
        if exc.errno == errno.ELOOP or getattr(exc, "winerror", 0) == 1921:
            raise
    return resolved


def normalize(source: str | Path, output: str | Path, profile: Profile,
              report_path: str | Path, tools: Toolchain | None = None, *,
              audio_stream: int | None = None, cancel_event: Event | None = None) -> dict[str, Any]:
    """Stage two-pass normalization, independently verify, then publish without overwrite.

    All expected failures return a structured report. Reports must have a fresh path.
    A report write failure is returned in report_error; it never leaves a new output.
    audio_stream is an absolute ffprobe stream index, required for multiple audio tracks.
    cancel_event is a caller-owned threading.Event: set and leave it set until return
    to request cancellation, including from another thread. It is never cleared here.
    """
    job = Job(600)
    source, output, report_path = map(lambda p: Path(p).absolute(),
                                      (source, output, report_path))
    report: dict[str, Any] = {
        "schema_version": 1, "status": "ERROR", "source": str(source),
        "output": str(output), "profile": _safe_profile(profile), "published": False,
        "policy": {"minimum_seconds": MIN_SECONDS, "output_sample_rate": OUTPUT_RATE,
                   "output_codec": "pcm_s24le", "peak_tolerance_db": 0,
                   "duration_tolerance_seconds": DURATION_TOLERANCE,
                   "metadata": "not copied; audio-only export"}, "commands": []}
    report_safe = False
    work = None
    output_identity = None

    def rollback_output() -> None:
        if output_identity is not None and output.exists():
            current = output.stat()
            if (current.st_dev, current.st_ino) == output_identity:
                output.unlink()

    try:
        # Resolve aliases before any execution or write, including report->source aliases.
        source_real, output_real, report_real = (_resolve_alias(p) for p in (source, output, report_path))
        if report_real in (source_real, output_real):
            report_safe = False
            raise AudioError("Source, output and report must be distinct paths")
        if report_path.exists() or report_path.is_symlink():
            report_safe = False
            raise AudioError("Report already exists; refusing to overwrite")
        report_safe = True
        if source_real == output_real:
            raise AudioError("Source and output must be distinct paths")
        if output.exists() or output.is_symlink():
            raise AudioError("Output already exists; refusing to overwrite")
        if not source.is_file():
            raise AudioError("Input must be an existing local regular file")
        if output.suffix.lower() != ".wav":
            raise AudioError("Output must have a .wav extension")
        if not output.parent.is_dir() or not report_path.parent.is_dir():
            raise AudioError("Output and report parent directories must already exist")
        if cancel_event is not None and not isinstance(cancel_event, Event):
            raise AudioError("cancel_event must be a threading.Event or None")
        job.cancel_event = cancel_event
        job.remaining()
        profile.validate()
        if audio_stream is not None and (type(audio_stream) is not int or audio_stream < 0):
            raise AudioError("audio_stream must be a nonnegative integer absolute stream index")
        report["requested_audio_stream"] = audio_stream
        configured = tools or Toolchain()
        configured.validate()
        job.deadline = job.started + round(configured.total_timeout * 1_000_000_000)
        report["policy"]["total_timeout_seconds"] = configured.total_timeout
        report["policy"]["process_timeout_seconds"] = configured.timeout
        runner = Runner(configured, job)
        report["commands"] = runner.commands
        report["ffmpeg_version"] = runner.run([runner.tools.ffmpeg, "-version"]).stdout.splitlines()[0]
        report["ffprobe_version"] = runner.run([runner.tools.ffprobe, "-version"]).stdout.splitlines()[0]
        # Copy once: all passes operate on the same source snapshot, never the original.
        work = tempfile.TemporaryDirectory(prefix=".loudnorm-", dir=output.parent)
        try:
            snapshot = Path(work.name) / "source.audio"
            staged = Path(work.name) / "candidate.wav"
            _copy_source(source, snapshot, job)
            report["source_sha256"] = sha256(snapshot, job)
            job.switch("analysis")
            report["input_media"] = probe(snapshot, runner, audio_stream=audio_stream)
            processing_source = snapshot
            source_map = f"0:{report['input_media']['stream_index']}"
            if report["input_media"]["format"] == "matroska,webm":
                # Decode only the selected track. Float64 PCM represents every supported
                # input sample without quantization; retain its original rate and channels.
                # This also removes container timestamps without padding/truncating audio.
                processing_source = Path(work.name) / "selected.wav"
                runner.run(runner.ffmpeg_prefix() + [
                    "-i", str(snapshot), "-map", source_map, "-vn", "-sn", "-dn",
                    "-map_metadata", "-1", "-map_chapters", "-1", "-c:a", "pcm_f64le",
                    "-threads", "1", "-n", str(processing_source)])
                decoded = probe(processing_source, runner)
                report["decoded_input_media"] = decoded
                report["input_media"]["duration_seconds"] = decoded["duration_seconds"]
                report["input_media"]["duration_source"] = "decoded selected PCM WAV sample count"
                if (decoded["channels"], decoded["sample_rate"]) != (
                        report["input_media"]["channels"], report["input_media"]["sample_rate"]):
                    raise AudioError("Selected-track decoding changed channels or sample rate")
                source_map = f"0:{decoded['stream_index']}"
            raw = parse_loudnorm(runner.run(runner.ffmpeg_prefix() + [
                "-i", str(processing_source), "-map", source_map, "-af",
                profile.filter() + ":print_format=json", "-threads", "1", "-f", "null", "-"
            ]).stderr)
            report["input_measurement"] = raw
            values = finite_measurement(raw)
            applied = {"measured_I": values["input_i"], "measured_TP": values["input_tp"],
                       "measured_LRA": values["input_lra"], "measured_thresh": values["input_thresh"],
                       "offset": values["target_offset"]}
            report["applied_parameters"] = {**applied, "linear": True}
            options = ":".join(f"{key}={value}" for key, value in applied.items())
            job.switch("encoding")
            result = runner.run(runner.ffmpeg_prefix() + [
                "-i", str(processing_source), "-map", source_map, "-vn", "-sn", "-dn",
                "-map_metadata", "-1", "-map_chapters", "-1",
                "-af", profile.filter() + ":" + options + ":linear=true:print_format=json",
                "-ar", str(OUTPUT_RATE), "-c:a", "pcm_s24le", "-threads", "1", "-n", str(staged)])
            processing = parse_loudnorm(result.stderr)
            report["processing_measurement"] = processing
            mode = processing.get("normalization_type")
            if mode not in {"linear", "dynamic"}:
                raise AudioError("FFmpeg did not report a known normalization_type")
            report["normalization_type"] = mode
            report["dynamic_fallback"] = mode == "dynamic"
            # Separate process reads the encoded WAV; filter's output_i is NOT the gate.
            job.switch("verification")
            report["output_media"] = probe(staged, runner, candidate=True)
            raw_output = parse_loudnorm(runner.run(runner.ffmpeg_prefix() + [
                "-i", str(staged), "-map", "0:a:0", "-af", profile.filter() + ":print_format=json",
                "-threads", "1", "-f", "null", "-"
            ]).stderr)
            report["output_measurement"] = raw_output
            checks = gate(raw_output, profile)
            checks["duration_pass"] = abs(report["output_media"]["duration_seconds"] -
                                           report["input_media"]["duration_seconds"]) <= DURATION_TOLERANCE
            checks["channels_pass"] = (report["output_media"]["channels"] ==
                                        report["input_media"]["channels"])
            report["checks"] = checks
            report["candidate_sha256"] = sha256(staged, job)
            if not all(checks[key] for key in ("lufs_pass", "peak_pass", "duration_pass", "channels_pass")):
                raise AudioError("Encoded artifact failed independent verification", "FAIL")
            # Detect source edits while processing; do not publish a stale snapshot unknowingly.
            if sha256(source, job) != report["source_sha256"]:
                raise AudioError("Source changed during processing; refusing publication")
            job.switch("publication")
            job.remaining()
            report["status"] = "PASS"
            report["published"] = True
            # link() is atomic, same-filesystem and no-clobber even across concurrent writers.
            identity = staged.stat()
            output_identity = (identity.st_dev, identity.st_ino)
            os.link(staged, output)
            job.remaining()
        finally:
            # Preserve the processing phase for diagnostics before timing cleanup.
            failed_stage = job.stage
            job.switch("cleanup")
            try:
                work.cleanup()
            except BaseException:
                failed_stage = "cleanup"
                raise
        job.switch("publication")
        job.remaining()
        report["timings"] = job.timings()
        _write_report(report_path, report, job)
        return report
    except (AudioError, OSError, ValueError, KeyboardInterrupt) as exc:
        # Cancellation and late report/deadline failures also roll back verified audio.
        rollback_output()
        if work:
            # Retry if the first cancellation interrupted directory removal itself.
            work.cleanup()
        report["status"] = exc.status if isinstance(exc, AudioError) else "ERROR"
        report["published"] = False
        report["error"] = "Job cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
        if isinstance(exc, OSError):
            report["errno"] = exc.errno
        report["failed_stage"] = failed_stage if job.stage == "cleanup" and work else job.stage
        if isinstance(exc, DeadlineExceeded):
            report["error_kind"] = "deadline_exceeded"
        elif isinstance(exc, (JobCancelled, KeyboardInterrupt)):
            report["error_kind"] = "cancelled"
        report["timings"] = job.timings()
        if report_safe:
            try:
                # Safety cleanup and diagnostic delivery are allowed after expiry.
                _write_report(report_path, report)
            except (OSError, ValueError) as report_exc:
                report["report_error"] = str(report_exc)
                if isinstance(report_exc, OSError):
                    report["report_errno"] = report_exc.errno
        else:
            report["report_error"] = "Unsafe or existing report destination; no report written"
        return report
