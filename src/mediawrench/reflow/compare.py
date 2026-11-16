"""Compare independently optimized profiles using stable source identities."""
from .io import srt_text
from .model import Config, InputError
from .optimize import reflow


def _boundaries(report):
    return {(a['unit_ids'][-1], b['unit_ids'][0]): {
                'cue_id': a['id'], 'next_cue_id': b['id'],
                'end_ms': a['end_ms'], 'next_start_ms': b['start_ms'],
                'gap_ms': b['start_ms'] - a['end_ms']}
            for a, b in zip(report['cues'], report['cues'][1:])}


def _constraints(report):
    return {(v['code'], tuple(v['unit_ids']), v.get('next_unit_id')): v
            for v in report['constraints']}


def _without_cue_id(violation):
    return {k: v for k, v in violation.items() if k != 'cue_id'}


def _summary(report):
    cue_fields = ('id', 'unit_ids', 'word_ids', 'text', 'start_ms', 'end_ms',
                  'fallback', 'line_widths', 'speaking_rate')
    return {'name': report['config']['profile_name'],
            **{k: report[k] for k in ('config', 'feasible', 'objective', 'metrics',
                                     'constraints', 'srt_roundtrip')},
            'cues': [{k: cue[k] for k in cue_fields} for cue in report['cues']]}


def compare_profiles(data, left_profile, right_profile, *, algorithm='dp', group_words=3,
                     **layout_options):
    """Resolve two names and their saved rate units with shared explicit options.

    Match boundaries by adjacent source-unit IDs and constraints by code, ordered
    unit IDs and next_unit_id. Cue IDs are navigation links, not comparison keys.
    Both results must survive the same SRT validation as ordinary CLI reflow.
    """
    left_config = Config.from_profile(left_profile, **layout_options)
    right_config = Config.from_profile(right_profile, **layout_options)
    if left_config.profile_name == right_config.profile_name:
        raise InputError('comparison profiles must have distinct names')
    left = reflow(data, left_config, algorithm=algorithm, group_words=group_words)
    right = reflow(data, right_config, algorithm=algorithm, group_words=group_words)
    srt_text(left)
    srt_text(right)
    order = {u['id']: i for i, u in enumerate(left['source_units'])}
    left_boundaries, right_boundaries = _boundaries(left), _boundaries(right)
    boundary_differences = []
    # Source times are fixed, so a boundary present on both sides is unchanged
    # even if a preceding partition changed the local cue numbers.
    for key in sorted(left_boundaries.keys() ^ right_boundaries.keys(), key=lambda k: order[k[0]]):
        boundary_differences.append({'after_unit_id': key[0], 'before_unit_id': key[1],
                                    'left': left_boundaries.get(key), 'right': right_boundaries.get(key)})
    left_constraints, right_constraints = _constraints(left), _constraints(right)
    constraint_differences = []
    keys = left_constraints.keys() | right_constraints.keys()
    for key in sorted(keys, key=lambda k: (tuple(order[u] for u in k[1]), k[0], k[2] or '')):
        a, b = left_constraints.get(key), right_constraints.get(key)
        if a is not None and b is not None and _without_cue_id(a) == _without_cue_id(b):
            continue
        change = {'code': key[0], 'unit_ids': list(key[1]),
                  'status': 'right_only' if a is None else 'left_only' if b is None else 'changed',
                  'left': a, 'right': b}
        if key[2] is not None:
            change['next_unit_id'] = key[2]
        constraint_differences.append(change)
    return {'format_version': 1, 'tool': 'wordcue-reflow', 'mode': 'profile_comparison',
            **{k: left[k] for k in ('algorithm', 'baseline_group_words', 'schema',
                'constraint_policy', 'timing_policy', 'source_units', 'normalized_words',
                'source_segments', 'source_text', 'diagnostics')},
            'profiles': {'left': _summary(left), 'right': _summary(right)},
            'boundary_differences': boundary_differences,
            'constraint_differences': constraint_differences}
