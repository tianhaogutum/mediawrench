"""Conservative contiguous text loops; source offsets never address normalized text."""

import unicodedata

from .core import AuditError, _json_value


DEFAULT_RULE = {
    'unit': 'words',
    'min_pattern_units': 2,
    'max_pattern_tokens': None,
    'min_repetitions': 3,
    'high_severity_repetitions': 6,
}
POLICY = {
    'offset_unit': 'unicode_code_points',
    'range_semantics': 'zero_based_half_open',
    'word_equality': 'NFKC and casefold per lexical token; punctuation/symbols exact',
    'character_equality': 'exact non-whitespace Unicode code points',
    'pattern_units': 'lexical tokens in words mode; lexical code points in characters mode; punctuation/symbols participate in equality but not minimum length',
    'whitespace': 'ignored between tokens; retained in occurrence source text',
    'selection': 'leftmost non-overlapping runs; longest complete coverage, shortest period on ties; minimum applies to primitive pattern',
    'search_bound': 'max_pattern_tokens is an inclusive period limit counting all non-whitespace tokens, including punctuation/symbols; null searches all feasible periods; selection is among searched periods; entire segment and all repetitions are scanned',
    'assessment': 'suspected_text_pattern',
    'severity_basis': 'repeat count only, not hallucination probability',
    'language_matching': 'exact explicit segment language, else document language, else default; no inference or region fallback',
}


def _rule(overrides, base, path):
    if not isinstance(overrides, dict):
        raise AuditError(f'{path}: expected rule object')
    for key in overrides.keys() - DEFAULT_RULE.keys():
        raise AuditError(f'{path}.{key}: unknown loop rule field')
    rule = {**base, **overrides}
    if rule['unit'] not in ('words', 'characters'):
        raise AuditError(f'{path}.unit: expected words or characters')
    for key, minimum in (('min_pattern_units', 1), ('min_repetitions', 2),
                         ('high_severity_repetitions', rule['min_repetitions'])):
        value = rule[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise AuditError(f'{path}.{key}: expected integer >= {minimum}')
    maximum = rule['max_pattern_tokens']
    if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1):
        raise AuditError(f'{path}.max_pattern_tokens: expected null or integer >= 1')
    return rule


def normalize_loop_config(config):
    """Resolve partial rules without modifying caller data; reject typos eagerly."""
    if config is None:
        config = {}
    _json_value(config, 'loop_config')
    if not isinstance(config, dict):
        raise AuditError('loop_config: expected object with default/languages')
    for key in config.keys() - {'default', 'languages'}:
        raise AuditError(f'loop_config.{key}: unknown configuration field')
    default = _rule(config.get('default', {}), DEFAULT_RULE, 'loop_config.default')
    languages = config.get('languages', {})
    if not isinstance(languages, dict):
        raise AuditError('loop_config.languages: expected language-to-rule object')
    resolved = {}
    for language, overrides in languages.items():
        if not language or language.strip() != language:
            raise AuditError('loop_config.languages: language keys must be nonempty without surrounding whitespace')
        resolved[language] = _rule(overrides, default, f'loop_config.languages.{language}')
    return {'default': default, 'languages': resolved}


def loop_settings(document, segment, config):
    # Language remains optional metadata. Non-string/empty values cannot select
    # a rule; preserve them in source_document rather than guessing a language.
    language, origin = None, None
    for record, location in ((segment, 'segment'), (document, 'document')):
        value = record.get('language')
        if isinstance(value, str) and value:
            language, origin = value, location
            break
    rule_source = f'languages.{language}' if language in config['languages'] else 'default'
    rule = config['languages'].get(language, config['default'])
    return {**rule, 'language': language, 'language_source': origin, 'rule_source': rule_source}


def _lexical(char):
    # Marks stay with their words, including decomposed accents. This is a
    # deterministic lexical tokenizer, not a language-specific word segmenter.
    return unicodedata.category(char)[0] in 'LNM' or unicodedata.category(char) == 'Pc'


def _tokens(text, unit):
    tokens = []
    start = 0
    while start < len(text):
        char = text[start]
        if char.isspace():
            start += 1
            continue
        lexical = _lexical(char)
        end = start + 1
        if unit == 'words' and lexical:
            while end < len(text) and _lexical(text[end]):
                end += 1
        raw = text[start:end]
        key = unicodedata.normalize('NFKC', raw).casefold() if unit == 'words' and lexical else raw
        tokens.append((key, lexical, start, end))
        start = end
    return tokens


def find_loops(text, settings):
    """Return loops and search coverage in O(n*B) comparisons/O(n) memory.

    B is the inclusive searched period limit (floor(n/minimum) when uncapped).
    For every searched period, a backwards equality streak yields all complete repeat
    counts without repeatedly comparing slices. Select maximal coverage first,
    then shortest period, before applying the minimum pattern size: otherwise
    a one-word repeat could masquerade as a two-word repeated phrase.
    """
    tokens = _tokens(text, settings['unit'])
    size = len(tokens)
    minimum = settings['min_repetitions']
    possible = size // minimum
    maximum = settings['max_pattern_tokens']
    bound = possible if maximum is None else min(possible, maximum)
    search = {'token_count': size, 'max_possible_pattern_tokens': possible,
              'searched_max_pattern_tokens': bound, 'limited_by_config': bound < possible}
    best = [None] * size
    lexical_prefix = [0]
    for token in tokens:
        lexical_prefix.append(lexical_prefix[-1] + token[1])
    for width in range(1, bound + 1):
        equal = 0
        for start in range(size - width - 1, -1, -1):
            equal = equal + 1 if tokens[start][:2] == tokens[start + width][:2] else 0
            count = 1 + equal // width
            if count < minimum:
                continue
            span = count * width
            previous = best[start]
            if previous is None or span > previous[0]:
                # Periods are visited shortest first; equal coverage keeps the
                # primitive period instead of a multiple of the same pattern.
                best[start] = (span, width, count)
    result = []
    start = 0
    while start < size:
        match = best[start]
        if match is None:
            start += 1
            continue
        span, width, count = match
        units = lexical_prefix[start + width] - lexical_prefix[start]
        if units < settings['min_pattern_units']:
            start += 1
            continue
        occurrences = []
        for index in range(count):
            left = tokens[start + index * width][2]
            right = tokens[start + (index + 1) * width - 1][3]
            occurrences.append({'start': left, 'end': right, 'text': text[left:right]})
        result.append({
            'start': occurrences[0]['start'], 'end': occurrences[-1]['end'],
            'unit': settings['unit'], 'pattern_tokens': [t[0] for t in tokens[start:start + width]],
            'pattern_units': units, 'repeat_count': count,
            'occurrences': occurrences,
            'severity': 'high' if count >= settings['high_severity_repetitions'] else 'medium',
            'assessment': 'suspected_text_pattern',
        })
        start += span
    return result, search
