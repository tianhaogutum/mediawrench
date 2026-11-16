"""Validate caller-declared compositions; never infer source/target relations."""

from copy import deepcopy

from .formats import BridgeError, Cue, validate_text


MAPPING_SCHEMA = 'audacity-subtitle-bridge-mapping/1'


def reconcile_mapping(mapping, sidecar, by_id, rows, labels_hash, projection, report):
    """Return mapped rows and consumed source IDs after validating every rule.

    Whitespace separators are explicit structural boundaries. Splits reconstruct
    the entire original payload, so no nonseparator text can disappear.
    """
    if by_id is None:
        raise BridgeError('A mapping requires a sidecar')
    if (not isinstance(mapping, dict) or set(mapping) != {
            'schema', 'sidecar_integrity_sha256', 'labels_sha256', 'operations'}
            or mapping['schema'] != MAPPING_SCHEMA):
        raise BridgeError('Invalid mapping schema/fields; expected ' + MAPPING_SCHEMA)
    if mapping['sidecar_integrity_sha256'] != sidecar['integrity_sha256']:
        raise BridgeError('Mapping sidecar hash mismatch; use the original matching sidecar')
    if mapping['labels_sha256'] != labels_hash:
        raise BridgeError('Mapping labels hash mismatch; review target line numbers and regenerate mapping')
    operations = mapping['operations']
    if not isinstance(operations, list) or not operations:
        raise BridgeError('Mapping operations must be a nonempty list')
    row_by_line = {row['line']: row for row in rows}
    positions = {cue_id: i for i, cue_id in enumerate(by_id)}
    mapped, consumed = {}, set()
    report['mapping'] = deepcopy(mapping)
    for number, operation in enumerate(operations, 1):
        context = f'Mapping operation {number}'
        if not isinstance(operation, dict):
            raise BridgeError(context + ': expected an object')
        kind = operation.get('kind')
        fields = ({'kind', 'source', 'separator', 'targets'} if kind == 'split' else
                  {'kind', 'sources', 'separator', 'target'} if kind == 'merge' else None)
        if fields is None or set(operation) != fields:
            raise BridgeError(context + ': invalid split/merge fields')
        separator = operation['separator']
        if not isinstance(separator, str) or any(c not in ' \t\n' for c in separator):
            raise BridgeError(context + ': separator must contain only spaces, tabs or newlines (or be empty)')
        ids = [operation['source']] if kind == 'split' else operation['sources']
        if (not isinstance(ids, list) or not ids or kind == 'merge' and len(ids) < 2
                or any(not isinstance(cue_id, str) or cue_id not in by_id for cue_id in ids)):
            raise BridgeError(context + ': invalid or unknown source cue IDs')
        if len(set(ids)) != len(ids) or consumed.intersection(ids):
            raise BridgeError(context + ': source cue already consumed by a mapping')
        records = [by_id[cue_id] for cue_id in ids]
        if kind == 'split':
            targets = operation['targets']
            if (not isinstance(targets, list) or len(targets) < 2
                    or any(not isinstance(t, dict) or set(t) != {'line', 'text'} for t in targets)):
                raise BridgeError(context + ': split requires at least two line/text targets')
            for target in targets:
                validate_text(target['text'])
            if separator.join(t['text'] for t in targets) != records[0]['text']:
                raise BridgeError(context + ': split targets and separator must reconstruct the entire original text')
        else:
            indexes = [positions[cue_id] for cue_id in ids]
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
                raise BridgeError(context + ': merge sources must be adjacent in original cue order')
            if any(record['metadata'] != records[0]['metadata'] for record in records[1:]):
                raise BridgeError(context + ': incompatible source metadata; refusing to choose presentation settings')
            targets = [{'line': operation['target'], 'text': separator.join(r['text'] for r in records)}]
            validate_text(targets[0]['text'])
        last_line = 0
        for target in targets:
            line = target['line']
            if type(line) is not int or line not in row_by_line or line <= last_line:
                raise BridgeError(context + ': target lines must exist and be in strictly increasing order')
            last_line = line
            if line in mapped:
                raise BridgeError(context + ': target label already consumed by a mapping')
            row = row_by_line[line]
            if row['label'].startswith('[cue:') or row['label'] != projection(target['text']):
                raise BridgeError(context + ': mapped label payload must equal the text projection with no [cue:ID] prefix')
            cue = Cue(records[0]['source_id'] if kind == 'split' else None,
                      row['start_ms'], row['end_ms'], target['text'], deepcopy(records[0]['metadata']))
            mapped[line] = (cue, {'mapping_operation': number, 'source_ids': list(ids),
                                 **({'source_identifiers': [r['source_id'] for r in records]}
                                    if kind == 'merge' else {}),
                                 'text': target['text']})
        consumed.update(ids)
        report['actions'].append({'kind': kind, 'mapping_operation': number,
                                  'sources': deepcopy(records), 'separator': separator,
                                  'targets': deepcopy(targets)})
        if kind == 'merge' and sidecar['source']['format'] == 'vtt' and any(r['source_id'] is not None for r in records):
            report['losses'].append({'kind': 'merged_cue_identifiers', 'mapping_operation': number,
                                     'source_ids': list(ids),
                                     'source_identifiers': [r['source_id'] for r in records],
                                     'reason': 'Merged cue has no single original identifier; all sources retained in actions'})
            report['warnings'].append(f'{context}: merged WebVTT identifiers retained in audit; output cue has no identifier')
    return mapped, consumed
