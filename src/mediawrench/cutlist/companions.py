# ruff: noqa: E701, E702
"""Compare explicit companions to saved cuts; no replay or editorial audit."""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import io
import json
import math
from pathlib import Path
import re

from .core import CutlistError, _unique_object
from .export import _frame_seconds, _seconds
from .frames import SECONDS_SERIALIZATION_ERROR, _frame_rate, exact_ratio

_SAFE = 2**53 - 1
_NUMBER = re.compile(r'-?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')


def _decimal(token: str) -> Decimal:
    if len(token) > 128 or not _NUMBER.fullmatch(token):
        raise CutlistError('Seconds must be bounded finite decimal numbers')
    value = Decimal(token)
    if not value.is_finite() or abs(value.as_tuple().exponent) > 1000 or not 0 <= value <= _SAFE:
        raise CutlistError('Seconds must be nonnegative, finite and <= 2**53-1 (exponent <= 1000)')
    return value


def _invalid_constant(token: str) -> None:
    raise CutlistError(f'Non-finite JSON number: {token}')


def _read(path: Path) -> str:
    if not path.is_file():
        raise CutlistError(f'Input must be an existing regular file: {path}')
    try:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return stream.read()
    except UnicodeDecodeError as exc:
        raise CutlistError(f'Input is not valid UTF-8: {path}') from exc


def _json_number(token: str) -> Decimal:
    if len(token) > 128:
        raise CutlistError('JSON decimal token exceeds supported precision')
    value = Decimal(token)
    if not value.is_finite() or not math.isfinite(float(value)):
        raise CutlistError('Non-finite or overflowing JSON number')
    return value


def _json(path: Path) -> dict:
    text = _read(path)
    try:
        doc = json.loads(text, object_pairs_hook=_unique_object,
                         parse_float=_json_number, parse_constant=_invalid_constant)
    except (ValueError, InvalidOperation) as exc:
        raise CutlistError(f'Invalid JSON in {path}: {exc}') from exc
    if not isinstance(doc, dict):
        raise CutlistError(f'JSON must be an object: {path}')
    # Reject non-finite Decimal values even in metadata outside the compared fields.
    def finite(value):
        if isinstance(value, Decimal) and not value.is_finite():
            raise CutlistError(f'Non-finite JSON number in {path}')
        if isinstance(value, dict):
            for item in value.values(): finite(item)
        if isinstance(value, list):
            for item in value: finite(item)
    finite(doc)
    return doc


def _integer(value, field, *, minimum=0, maximum=_SAFE) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CutlistError(f'{field} must be an integer in {minimum}..{maximum}')
    return value


def _ratio(value, field) -> Fraction:
    if not isinstance(value, dict) or set(value) != {'numerator', 'denominator'}:
        raise CutlistError(f'{field} must be an exact rational object')
    n = _integer(value['numerator'], field + '.numerator', maximum=_SAFE * 10**9)
    d = _integer(value['denominator'], field + '.denominator', minimum=1, maximum=10**9)
    ratio = Fraction(n, d)
    if exact_ratio(ratio) != value:
        raise CutlistError(f'{field} must be a reduced rational')
    return ratio


def _string(value, field) -> str:
    if not isinstance(value, str):
        raise CutlistError(f'{field} must be a string')
    value.encode('utf-8')
    return value


def _media(value) -> str:
    value = _string(value, 'mediaFileName')
    if (not value.strip() or value in {'.', '..'} or any(c in value for c in '/\\')
            or any(ord(c) < 32 for c in value)):
        raise CutlistError('mediaFileName must be a plain filename')
    return value


def _saved(doc: dict):
    schema = _integer(doc.get('schemaVersion'), 'schemaVersion', minimum=1, maximum=2)
    media = _media(doc.get('mediaFileName'))
    selection = doc.get('normalizedSelection')
    if not isinstance(selection, dict):
        raise CutlistError('normalizedSelection must contain durationMs')
    duration = _integer(selection.get('durationMs'), 'durationMs', minimum=1)
    rate = None
    limit = duration
    if schema == 2:
        grid = doc.get('quantization')
        if not isinstance(grid, dict):
            raise CutlistError('Schema2 requires quantization')
        rate = _ratio(grid.get('frameRate'), 'frameRate')
        _frame_rate(f'{rate.numerator}/{rate.denominator}')
        if (_ratio(grid.get('timeBase'), 'timeBase') != 1 / rate
                or _integer(grid.get('originSeconds'), 'originSeconds') != 0
                or _ratio(grid.get('secondsSerializationMaxErrorInclusive'), 'serialization error')
                != SECONDS_SERIALIZATION_ERROR):
            raise CutlistError('Unsupported saved frame grid or serialization policy')
        limit = _integer(grid.get('durationFrameFloor'), 'durationFrameFloor', minimum=1)
        if limit != int(Fraction(duration, 1000) * rate):
            raise CutlistError('durationFrameFloor disagrees with durationMs and frameRate')
    cuts = doc.get('cuts')
    if not isinstance(cuts, list) or not cuts:
        raise CutlistError('Saved cuts must be a nonempty ordered array')
    saved = []
    previous = 0
    for index, cut in enumerate(cuts, 1):
        if not isinstance(cut, dict) or _integer(cut.get('cutId'), 'cutId', minimum=1) != index:
            raise CutlistError('Saved cutId must follow array order starting at 1')
        name = _string(cut.get('name'), 'cut name')
        edges = []
        units = []
        for edge in ('start', 'end'):
            unit = _integer(cut.get(edge + ('Ms' if rate is None else 'Frame')), edge)
            exact = Fraction(unit, 1000) if rate is None else unit / rate
            if rate is None:
                _seconds(unit)  # Require an exportable authoritative millisecond value.
            else:
                if _ratio(cut.get(edge + 'ExactSeconds'), edge + 'ExactSeconds') != exact:
                    raise CutlistError('Saved exact seconds disagree with frame boundary/rate')
                _frame_seconds(unit, rate)
            units.append(unit); edges.append(exact)
        if not previous <= units[0] < units[1] <= limit:
            raise CutlistError('Saved cuts must be ordered, disjoint, positive and within duration')
        previous = units[1]
        saved.append((*edges, name))
    return media, saved, rate


def _segment(start, end, name):
    if type(start) not in (int, Decimal) or type(end) not in (int, Decimal):
        raise CutlistError('Companion boundaries must be numeric seconds')
    start, end = _decimal(str(start)), _decimal(str(end))
    if start >= end:
        raise CutlistError('Companion cut must have positive length')
    return start, end, _string(name, 'companion name')


def _llc(doc):
    _integer(doc.get('version'), 'LLC version', minimum=2, maximum=2)
    media = _media(doc.get('mediaFileName'))
    segments = doc.get('cutSegments')
    if not isinstance(segments, list) or any(not isinstance(s, dict) for s in segments):
        raise CutlistError('LLC cutSegments must be an array of objects')
    return media, [_segment(s.get('start'), s.get('end'), s.get('name')) for s in segments]


def _csv(text):
    # csv.reader(strict=True) permits stray quotes inside unquoted fields. Reject
    # those too, while retaining quoted commas/newlines and doubled quote escapes.
    state = 'start'
    for char in text:
        if state == 'quoted':
            if char == '"': state = 'closed'
        elif state == 'closed':
            if char == '"': state = 'quoted'
            elif char in ',\r\n': state = 'start'
            else: raise CutlistError('Invalid CSV after closing quote')
        elif char == '"':
            if state != 'start': raise CutlistError('Invalid CSV quote in unquoted field')
            state = 'quoted'
        elif char in ',\r\n': state = 'start'
        else: state = 'unquoted'
    if state == 'quoted': raise CutlistError('Unterminated quoted CSV field')
    result = []
    try:
        for row in csv.reader(io.StringIO(text, newline=''), strict=True):
            if len(row) != 3:
                raise CutlistError('CSV requires exactly start,end,name (no header or blank rows)')
            result.append(_segment(_decimal(row[0]), _decimal(row[1]), row[2]))
    except csv.Error as exc:
        raise CutlistError(f'Invalid CSV: {exc}') from exc
    return result


def verify_companions(provenance_path: str | Path, *, llc_path: str | Path,
                      csv_path: str | Path) -> dict:
    """Read explicit schema1/2 provenance, LLC v2 and seconds CSV; never execute.

    Returns matches/mismatch details. Malformed/unsupported inputs raise
    CutlistError; unreadable files may raise OSError. This is not authentication.
    """
    if llc_path is None or csv_path is None:
        raise CutlistError('Explicit --llc and --csv paths must be supplied together')
    provenance, llc, csv_file = map(Path, (provenance_path, llc_path, csv_path))
    media, cuts, rate = _saved(_json(provenance))
    actual_media, llc_cuts = _llc(_json(llc))
    csv_cuts = _csv(_read(csv_file))
    issues = []
    files = {}
    for kind, path, actual in [('llc', llc, llc_cuts), ('csv', csv_file, csv_cuts)]:
        files[kind] = {'path': str(path.absolute()), 'cutCount': len(actual),
                       'mediaReference': 'represented' if kind == 'llc' else 'not-represented'}

        def issue(code, field, expected, value, index=None):
            row = {'code': code, 'file': kind, 'path': str(path.absolute()), 'field': field,
                   'expected': expected, 'actual': value}
            if index is not None: row['cutIndex'] = index
            issues.append(row)

        if kind == 'llc' and media != actual_media:
            issue('media-file-name', 'mediaFileName', media, actual_media)
        if len(cuts) != len(actual):
            issue('cut-count', 'cutCount', len(cuts), len(actual))
        for index, (expected, found) in enumerate(zip(cuts, actual), 1):
            for field, exact, value in zip(('start', 'end'), expected[:2], found[:2]):
                numeric = Fraction(value)
                matches = numeric == exact if rate is None else (
                    abs(numeric - exact) <= SECONDS_SERIALIZATION_ERROR
                    and int(numeric * rate + Fraction(1, 2)) == exact * rate)
                if not matches:
                    issue('cut-' + field, field, exact_ratio(exact), str(value), index)
            if expected[2] != found[2]:
                issue('cut-name', 'name', expected[2], found[2], index)
    return {'schemaVersion': 1, 'command': 'verify-companions',
            'scope': 'saved-cuts-and-explicit-companions',
            'status': 'mismatch' if issues else 'matched', 'matches': not issues,
            'replayed': False, 'provenancePath': str(provenance.absolute()),
            'expectedCutCount': len(cuts), 'files': files, 'issues': issues}
