"""Opt-in local ffprobe duration/rate/origin evidence; no PATH lookup or cutting."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .core import Cue, CutlistError, Timeline, _milliseconds, _unique_object, build_timeline
from .frames import _frame_rate, exact_ratio


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_ms(value: object) -> int:
    # ffprobe's JSON writer emits duration as a decimal string. Missing values,
    # N/A, non-finite numbers and alternate types must never trigger a fallback.
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value)):
        raise CutlistError('ffprobe format.duration must be a positive decimal seconds string')
    with localcontext() as context:
        context.prec = 80
        exact_ms = Decimal(value) * 1000
        if not 1 <= exact_ms <= 2**53 - 1:
            raise CutlistError('ffprobe format.duration must be at least 1 ms and fit a safe integer')
        # Positive int truncation is floor: exported cuts never exceed evidence.
        return int(exact_ms)


def _stream_index(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**31:
        raise CutlistError('Video stream index must be an integer in [0, 2147483647]')
    return value


def _probe_argv(executable: str, media: str, stream_index: int | None = None,
                verify_zero_origin: bool = False) -> list[str]:
    entries = 'format=duration,format_name,size:program_version=version'
    selection = []
    if stream_index is not None:
        selection = ['-select_streams', str(_stream_index(stream_index))]
        entries += ':stream=index,codec_type,avg_frame_rate,r_frame_rate'
        if verify_zero_origin:
            entries += ',start_time'
    return [executable, '-v', 'error', '-protocol_whitelist', 'file', *selection,
            '-show_program_version', '-show_entries', entries,
            '-of', 'json', '-i', media]


def _strict_json(text: str) -> object:
    def invalid_number(value: str) -> None:
        raise CutlistError(f'Non-finite JSON number: {value}')

    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=invalid_number)


def _probe_response(stdout: bytes) -> tuple[str, int, str]:
    try:
        response = _strict_json(stdout.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise CutlistError(f'Invalid ffprobe JSON response: {exc}') from exc
    if not isinstance(response, dict) or not isinstance(response.get('format'), dict):
        raise CutlistError('ffprobe response lacks format.duration')
    duration = response['format'].get('duration')
    duration_ms = _duration_ms(duration)
    version = response.get('program_version')
    if not isinstance(version, dict) or not isinstance(version.get('version'), str) or not version['version']:
        raise CutlistError('ffprobe response lacks program_version.version')
    return duration, duration_ms, version['version']


def _selected_video_stream(stdout: bytes, stream_index: int) -> dict:
    response = _strict_json(stdout.decode('utf-8'))
    streams = response.get('streams') if isinstance(response, dict) else None
    if (not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict)
            or type(streams[0].get('index')) is not int or streams[0]['index'] != stream_index
            or streams[0].get('codec_type') != 'video'):
        raise CutlistError(f'ffprobe must return exactly the selected video stream index {stream_index}')
    return streams[0]


def _frame_rate_validation(stdout: bytes, stream_index: int, declared: str) -> dict:
    """Compare metadata exactly; this does not establish decoded CFR timing."""
    rate = _frame_rate(declared)
    stream = _selected_video_stream(stdout, stream_index)
    for key in ('avg_frame_rate', 'r_frame_rate'):
        try:
            actual = _frame_rate(stream.get(key))
        except CutlistError as exc:
            raise CutlistError(f'ffprobe video stream {stream_index} has unusable {key}: {stream.get(key)!r}') from exc
        if actual != rate:
            raise CutlistError(f'Video frame rate mismatch for stream {stream_index}: '
                               f'declared {declared}, {key}={stream[key]}')
    return {'streamIndex': stream_index, 'declaredFrameRate': declared,
            'avgFrameRate': stream['avg_frame_rate'], 'rFrameRate': stream['r_frame_rate'],
            'policy': 'exact-avg-and-r-frame-rate'}


def _start_time_seconds(value: object) -> str:
    # Plain decimal seconds as emitted by ffprobe, with no float conversion,
    # millisecond rounding, exponent, ratio, tolerance or missing-value fallback.
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value)):
        raise CutlistError('ffprobe stream.start_time must be a known signed decimal seconds string')
    return value


def _zero_origin_validation(stdout: bytes, stream_index: int) -> dict:
    stream = _selected_video_stream(stdout, stream_index)
    start = _start_time_seconds(stream.get('start_time'))
    if Decimal(start) != 0:
        raise CutlistError(f'Video stream {stream_index} start_time must be exactly zero: {start}')
    return {'streamIndex': stream_index, 'startTimeSeconds': start,
            'policy': 'exact-zero-stream-start-time'}


def build_probed_timeline(cues: tuple[Cue, ...], plan: dict, *,
                          media_path: str | Path, ffprobe_path: str | Path,
                          video_stream_index: int | None = None,
                          verify_zero_origin: bool = False) -> Timeline:
    """Build using format.duration from one explicit local ffprobe invocation.

    The original plan stays in provenance. If it declares duration, it must equal
    the probed bound floored to milliseconds. All existing cue/plan checks apply.
    Media and executable must be regular files; URLs, stdin and PATH search are
    unsupported. The argv and raw response in durationProbe can be replayed with
    the same unchanged files and executable. Nothing here writes to media.
    video_stream_index opts into exact avg/r rate checks against plan.frameRate
    for that absolute stream index. It validates metadata, not decoded cadence.
    verify_zero_origin also requires its known start_time to be exactly zero;
    it needs video_stream_index and never shifts or infers timestamps.
    """
    if not isinstance(plan, dict):
        raise CutlistError('Selection plan must be a JSON object')
    if type(verify_zero_origin) is not bool:
        raise CutlistError('verify_zero_origin must be a boolean')
    if verify_zero_origin and video_stream_index is None:
        raise CutlistError('verify_zero_origin requires video_stream_index and plan frameRate')
    if video_stream_index is not None:
        _stream_index(video_stream_index)
        _frame_rate(plan.get('frameRate'))
    try:
        executable = Path(ffprobe_path).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise CutlistError(f'ffprobe must be an executable regular file: {executable}')
    except OSError as exc:
        raise CutlistError(f'Cannot access explicit ffprobe executable: {exc}') from exc
    try:
        media = Path(media_path).resolve(strict=True)
        if not media.is_file():
            raise CutlistError(f'Probe media must be a local regular file: {media}')
        media_hash = _sha256(media)
        executable_hash = _sha256(executable)
    except OSError as exc:
        raise CutlistError(f'Cannot read local probe input: {exc}') from exc
    argv = _probe_argv(str(executable), str(media), video_stream_index, verify_zero_origin)
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise CutlistError('ffprobe timed out after 15 seconds; no duration inferred') from exc
    except OSError as exc:
        raise CutlistError(f'Cannot execute explicit ffprobe: {exc}') from exc
    if result.returncode:
        message = result.stderr.decode('utf-8', errors='replace').strip()
        raise CutlistError(f'ffprobe failed (exit {result.returncode}): {message[:1000]}')
    raw_duration, duration_ms, version = _probe_response(result.stdout)
    validation = (_frame_rate_validation(result.stdout, video_stream_index, plan['frameRate'])
                  if video_stream_index is not None else None)
    origin = (_zero_origin_validation(result.stdout, video_stream_index)
              if verify_zero_origin else None)
    stdout = result.stdout.decode('utf-8')
    if 'duration' in plan and _milliseconds(plan['duration'], 'duration') != duration_ms:
        raise CutlistError(f'Declared duration conflicts with ffprobe duration bound: {duration_ms} ms')
    seconds = duration_ms / 1000
    if _milliseconds(seconds, 'probed duration') != duration_ms:
        raise CutlistError('Probed duration cannot be represented without millisecond rounding')
    timeline = build_timeline(cues, {**plan, 'duration': seconds})
    evidence = {
        'schemaVersion': 3 if origin is not None else (2 if validation is not None else 1), 'argv': argv, 'version': version,
        'ffprobePath': str(executable), 'ffprobeSha256': executable_hash,
        'mediaPath': str(media), 'mediaSizeBytes': media.stat().st_size, 'mediaSha256': media_hash,
        'returnCode': result.returncode, 'stdout': stdout,
        'stdoutSha256': hashlib.sha256(result.stdout).hexdigest(),
        'stderr': result.stderr.decode('utf-8', errors='replace'),
        'durationSeconds': raw_duration, 'durationMs': duration_ms,
        'rounding': 'floor-to-milliseconds',
    }
    if validation is not None:
        evidence['frameRateValidation'] = validation
    if origin is not None:
        evidence['zeroOriginValidation'] = origin
    return replace(timeline, plan_json=json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False),
                   duration_probe_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _saved_evidence(document: object) -> dict:
    """Validate the supported shape and constrain argv before any execution."""
    if (not isinstance(document, dict) or type(document.get('schemaVersion')) is not int
            or document['schemaVersion'] not in (1, 2)):
        raise CutlistError('Expected saved provenance schemaVersion 1 or 2')
    evidence = document.get('durationProbe')
    if (not isinstance(evidence, dict) or type(evidence.get('schemaVersion')) is not int
            or evidence['schemaVersion'] not in (1, 2, 3)):
        raise CutlistError('Saved provenance requires durationProbe schemaVersion 1, 2 or 3')
    stream_index = None
    if evidence['schemaVersion'] in (2, 3):
        validation = evidence.get('frameRateValidation')
        if not isinstance(validation, dict) or validation.get('policy') != 'exact-avg-and-r-frame-rate':
            raise CutlistError('durationProbe schema 2/3 requires frameRateValidation with exact policy')
        stream_index = _stream_index(validation.get('streamIndex'))
        for key in ('declaredFrameRate', 'avgFrameRate', 'rFrameRate'):
            _frame_rate(validation.get(key))
        plan = document.get('selectionPlan')
        grid = document.get('quantization')
        if (document['schemaVersion'] != 2 or not isinstance(plan, dict)
                or not isinstance(grid, dict) or not isinstance(grid.get('frameRate'), dict)):
            raise CutlistError('Rate evidence requires frame provenance with selectionPlan and quantization')
        _frame_rate(plan.get('frameRate'))
        ratio = grid['frameRate']
        if any(type(ratio.get(key)) is not int or ratio[key] <= 0 for key in ('numerator', 'denominator')):
            raise CutlistError('quantization.frameRate must be a positive integer ratio')
    elif 'frameRateValidation' in evidence:
        raise CutlistError('frameRateValidation requires durationProbe schemaVersion 2 or 3')
    if evidence['schemaVersion'] == 3:
        origin = evidence.get('zeroOriginValidation')
        if not isinstance(origin, dict) or origin.get('policy') != 'exact-zero-stream-start-time':
            raise CutlistError('durationProbe schema 3 requires zeroOriginValidation with exact policy')
        if _stream_index(origin.get('streamIndex')) != stream_index:
            raise CutlistError('zeroOriginValidation streamIndex must match frameRateValidation')
        _start_time_seconds(origin.get('startTimeSeconds'))
        if type(document['quantization'].get('originSeconds')) is not int:
            raise CutlistError('Schema 3 requires quantization.originSeconds as an integer')
    elif 'zeroOriginValidation' in evidence:
        raise CutlistError('zeroOriginValidation requires durationProbe schemaVersion 3')
    for key in ('ffprobePath', 'mediaPath'):
        value = evidence.get(key)
        if not isinstance(value, str) or '\0' in value or not Path(value).is_absolute():
            raise CutlistError(f'durationProbe.{key} must be an absolute local path')
    # Do not blindly execute arbitrary arguments from a JSON file. Only the
    # recorder's exact file-only duration/rate query is supported, with no overrides.
    if evidence.get('argv') != _probe_argv(
            evidence['ffprobePath'], evidence['mediaPath'], stream_index, evidence['schemaVersion'] == 3):
        raise CutlistError('durationProbe.argv is not the supported file-only duration query')
    for key in ('ffprobeSha256', 'mediaSha256', 'stdoutSha256'):
        if not isinstance(evidence.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', evidence[key]):
            raise CutlistError(f'durationProbe.{key} must be a lowercase SHA-256 digest')
    for key in ('stdout', 'stderr', 'version'):
        if not isinstance(evidence.get(key), str):
            raise CutlistError(f'durationProbe.{key} must be a string')
    if not evidence['version']:
        raise CutlistError('durationProbe.version must be nonempty')
    for key, minimum in [('durationMs', 1), ('mediaSizeBytes', 0)]:
        if type(evidence.get(key)) is not int or evidence[key] < minimum:
            raise CutlistError(f'durationProbe.{key} must be an integer >= {minimum}')
    _duration_ms(evidence.get('durationSeconds'))
    if (evidence.get('rounding') != 'floor-to-milliseconds'
            or type(evidence.get('returnCode')) is not int or evidence['returnCode'] != 0):
        raise CutlistError('durationProbe requires floor-to-milliseconds rounding and successful returnCode 0')
    selection = document.get('normalizedSelection')
    if not isinstance(selection, dict) or type(selection.get('durationMs')) is not int or selection['durationMs'] < 1:
        raise CutlistError('Saved provenance requires normalizedSelection.durationMs as a positive integer')
    return evidence


def verify_probe_evidence(provenance_path: str | Path, *,
                          srt_path: str | Path | None = None,
                          plan_path: str | Path | None = None,
                          require_zero_origin: bool = False,
                          expected_video_stream_index: int | None = None,
                          expected_frame_rate: str | None = None,
                          expected_media_sha256: str | None = None,
                          expected_ffprobe_sha256: str | None = None,
                          preflight_only: bool = False) -> dict:
    """Check saved provenance, current file hashes, then replay its exact argv.

    Returns a read-only JSON-compatible report (verified, preflight-only or stale). Malformed or
    unsupported provenance raises CutlistError; unavailable files and failed or
    differing replay are stale. No replay occurs after a failed preflight check.
    This verifies duration and optional rate/origin evidence, not cuts or executable trust.
    Supply both source paths to also compare their bytes to saved source hashes
    before replay. No source paths are inferred and no plan is parsed/reapplied.
    Saved source digests are required/validated only when this check is requested.
    require_zero_origin rejects evidence without supported schema-3 origin proof
    before hashing current inputs or replay. It never upgrades weaker evidence;
    all existing schema-3 coherence and stale checks still apply.
    expected_video_stream_index binds the saved absolute selected video index
    (schema 2/3 only). A mismatch is stale before current-input hashing/replay;
    a match retains every existing check and never changes the recorded argv.
    expected_frame_rate binds the selected declared rate (schema 2/3 only),
    using the same bounded rational-string parser as plan.frameRate. Equivalent
    fractions match; a mismatch is stale before current-input hashing/replay.
    expected_media_sha256 binds the saved media digest on schemas 1/2/3. Only
    lowercase 64-character ASCII hex strings or None are accepted, before file
    access. A mismatch is stale before current-input hashing/replay; matching
    retains all checks and never retargets media or authenticates provenance.
    expected_ffprobe_sha256 similarly binds the saved executable digest on all
    supported schemas, with the same strict input and stale precedence. Matching
    retains current executable checks and exact argv; it does not establish trust.
    preflight_only is strictly boolean. True runs every saved-evidence and current
    file check, then returns status='preflight-only', scope='saved-evidence-and-current-files'
    and replayed=False without executing anything. Invalid/stale results retain
    their existing contracts. Success does not verify replay or guarantee later reuse.
    """
    if type(preflight_only) is not bool:
        raise CutlistError('preflight_only must be a boolean')
    if type(require_zero_origin) is not bool:
        raise CutlistError('require_zero_origin must be a boolean')
    if expected_video_stream_index is not None:
        _stream_index(expected_video_stream_index)
    expected_rate = _frame_rate(expected_frame_rate) if expected_frame_rate is not None else None
    for name, digest in [('expected_media_sha256', expected_media_sha256),
                         ('expected_ffprobe_sha256', expected_ffprobe_sha256)]:
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                   or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise CutlistError(f'{name} must be a lowercase SHA-256 digest (64 ASCII hex characters)')
    if (srt_path is None) != (plan_path is None):
        raise CutlistError('srt_path and plan_path must be supplied together')
    path = Path(provenance_path)
    try:
        if not path.is_file():
            raise CutlistError('Saved provenance must be a local regular file')
        document = _strict_json(path.read_text(encoding='utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise CutlistError(f'Invalid UTF-8 provenance JSON: {exc}') from exc
    evidence = _saved_evidence(document)
    if require_zero_origin and evidence['schemaVersion'] != 3:
        raise CutlistError('require_zero_origin requires durationProbe schemaVersion 3 with zero-origin validation')
    if expected_video_stream_index is not None and evidence['schemaVersion'] == 1:
        raise CutlistError('expected_video_stream_index requires durationProbe schemaVersion 2 or 3 with selected-video validation')
    if expected_rate is not None and evidence['schemaVersion'] == 1:
        raise CutlistError('expected_frame_rate requires durationProbe schemaVersion 2 or 3 with selected-rate validation')
    sources = []
    if srt_path is not None:
        source = document.get('source')
        if not isinstance(source, dict):
            raise CutlistError('Saved provenance requires a source object for source verification')
        for kind, current_path in [('srt', srt_path), ('plan', plan_path)]:
            key = kind + 'Sha256'
            digest = source.get(key)
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise CutlistError(f'source.{key} must be a lowercase SHA-256 digest')
            sources.append((kind, Path(current_path), digest))
    issues = []
    report = {'command': 'verify-probe-evidence', 'schemaVersion': 1,
              'provenancePath': str(path.resolve()), 'status': 'stale', 'stale': True,
              'replayed': False, 'durationMs': evidence['durationMs'], 'issues': issues}

    def issue(code: str, message: str, **details: object) -> None:
        issues.append({'code': code, 'message': message, **details})

    def compare(code: str, expected: object, actual: object) -> None:
        if expected != actual:
            issue(code, 'Recorded and current evidence differ', expected=expected, actual=actual)

    raw = b''
    try:
        raw = evidence['stdout'].encode('utf-8')
        compare('recorded-stdout-sha256', evidence['stdoutSha256'], hashlib.sha256(raw).hexdigest())
        seconds, duration, version = _probe_response(raw)
        compare('recorded-duration', evidence['durationSeconds'], seconds)
        compare('recorded-duration', evidence['durationMs'], duration)
        compare('recorded-version', evidence['version'], version)
    except (CutlistError, UnicodeError) as exc:
        issue('recorded-response-invalid', str(exc))
    compare('timeline-duration', document['normalizedSelection']['durationMs'], evidence['durationMs'])
    if evidence['schemaVersion'] in (2, 3):
        validation = evidence['frameRateValidation']
        try:
            actual = _frame_rate_validation(raw, validation['streamIndex'], validation['declaredFrameRate'])
            compare('recorded-frame-rate', validation, actual)
        except (ValueError, UnicodeError) as exc:
            issue('recorded-frame-rate', str(exc))
        declared = _frame_rate(validation['declaredFrameRate'])
        compare('recorded-frame-rate', exact_ratio(declared),
                exact_ratio(_frame_rate(document['selectionPlan']['frameRate'])))
        grid_rate = document['quantization']['frameRate']
        compare('recorded-frame-rate', exact_ratio(declared),
                exact_ratio(Fraction(grid_rate['numerator'], grid_rate['denominator'])))
    if evidence['schemaVersion'] == 3:
        compare('recorded-zero-origin', 0, document['quantization']['originSeconds'])
        try:
            actual = _zero_origin_validation(raw, evidence['zeroOriginValidation']['streamIndex'])
            compare('recorded-zero-origin', evidence['zeroOriginValidation'], actual)
        except (ValueError, UnicodeError) as exc:
            issue('recorded-zero-origin', str(exc))
    if expected_video_stream_index is not None:
        actual_index = evidence['frameRateValidation']['streamIndex']
        if expected_video_stream_index != actual_index:
            issue('expected-video-stream', 'Saved selected video stream differs from caller expectation',
                  expected=expected_video_stream_index, actual=actual_index)
    if expected_rate is not None:
        actual_rate = _frame_rate(evidence['frameRateValidation']['declaredFrameRate'])
        if expected_rate != actual_rate:
            issue('expected-frame-rate', 'Saved selected declared frame rate differs from caller expectation',
                  expected=exact_ratio(expected_rate), actual=exact_ratio(actual_rate))
    if expected_media_sha256 is not None and expected_media_sha256 != evidence['mediaSha256']:
        issue('expected-media-sha256', 'Saved media SHA-256 differs from caller expectation',
              expected=expected_media_sha256, actual=evidence['mediaSha256'])
    if expected_ffprobe_sha256 is not None and expected_ffprobe_sha256 != evidence['ffprobeSha256']:
        issue('expected-ffprobe-sha256', 'Saved ffprobe SHA-256 differs from caller expectation',
              expected=expected_ffprobe_sha256, actual=evidence['ffprobeSha256'])
    if issues:
        return report

    for kind, current, digest in sources:
        try:
            if not current.is_file():
                issue(kind + '-unavailable', f'Not an accessible {kind} regular file: {current}')
                continue
            compare(kind + '-sha256', digest, _sha256(current))
        except OSError as exc:
            issue(kind + '-unavailable', f'Cannot read {kind} source {current}: {exc}')
    if issues:
        return report

    for kind, path_key, hash_key in [('media', 'mediaPath', 'mediaSha256'),
                                     ('ffprobe', 'ffprobePath', 'ffprobeSha256')]:
        current = Path(evidence[path_key])
        try:
            if not current.is_file() or (kind == 'ffprobe' and not os.access(current, os.X_OK)):
                issue(kind + '-unavailable', f'Not an accessible {kind} regular file: {current}')
                continue
            if kind == 'media':
                compare('media-size', evidence['mediaSizeBytes'], current.stat().st_size)
            compare(kind + '-sha256', evidence[hash_key], _sha256(current))
        except OSError as exc:
            issue(kind + '-unavailable', str(exc))
    if issues:
        return report

    if preflight_only:
        report.update(status='preflight-only', scope='saved-evidence-and-current-files', stale=False)
        return report

    report['replayed'] = True
    try:
        result = subprocess.run(evidence['argv'], stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        issue('replay-timeout', 'ffprobe timed out after 15 seconds; duration evidence is stale')
        return report
    except OSError as exc:
        issue('replay-failed', f'Cannot execute recorded ffprobe: {exc}')
        return report
    if result.returncode:
        issue('replay-failed', 'Recorded ffprobe query failed', returnCode=result.returncode,
              stderr=result.stderr.decode('utf-8', errors='replace')[:1000])
        return report
    compare('replay-stdout-sha256', evidence['stdoutSha256'], hashlib.sha256(result.stdout).hexdigest())
    compare('replay-stderr', evidence['stderr'], result.stderr.decode('utf-8', errors='replace'))
    try:
        seconds, duration, version = _probe_response(result.stdout)
        report['replayDurationMs'] = duration
        compare('replay-duration', evidence['durationSeconds'], seconds)
        compare('replay-duration', evidence['durationMs'], duration)
        compare('replay-version', evidence['version'], version)
    except CutlistError as exc:
        issue('replay-invalid', str(exc))
    if evidence['schemaVersion'] in (2, 3):
        validation = evidence['frameRateValidation']
        try:
            current = _frame_rate_validation(result.stdout, validation['streamIndex'], validation['declaredFrameRate'])
            compare('replay-frame-rate', validation, current)
        except (CutlistError, UnicodeError, ValueError) as exc:
            issue('replay-frame-rate', str(exc))
    if evidence['schemaVersion'] == 3:
        try:
            current = _zero_origin_validation(result.stdout, evidence['zeroOriginValidation']['streamIndex'])
            compare('replay-zero-origin', evidence['zeroOriginValidation'], current)
        except (ValueError, UnicodeError) as exc:
            issue('replay-zero-origin', str(exc))
    if not issues:
        report.update(status='verified', stale=False)
    return report
