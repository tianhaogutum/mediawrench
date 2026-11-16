"""Template reconciliation choices validate inputs without changing mapping/1."""

import copy
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.audacity import (
    BridgeError, create_mapping_template, export_subtitles, import_labels,
)
from mediawrench.audacity.formats import parse_srt, parse_vtt

ROOT = Path(__file__).resolve().parents[2]


def fixture(fmt):
    if fmt == 'srt':
        source = ('7\n00:00:01,000 --> 00:00:03,000\nHello\nworld\n\n'
                  '13\n00:00:04,000 --> 00:00:05,000\nplain\n\n'
                  '15\n00:00:06,000 --> 00:00:07,000\nmissing\n\n'
                  '9\n00:00:08,000 --> 00:00:09,000\nAgain\n\n'
                  '11\n00:00:10,000 --> 00:00:11,000\nLater\n\n')
        ids = ['7', '9', '11', '13', '15']
    else:
        source = ('WEBVTT header\n\nNOTE preamble\n\n'
                  'speaker\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
                  'p\n00:04.000 --> 00:05.000\nplain\n\n'
                  'm\n00:06.000 --> 00:07.000\nmissing\n\n'
                  'a\n00:08.000 --> 00:09.000 align:end\nAgain\n\n'
                  'b\n00:10.000 --> 00:11.000 align:end\nLater\n')
        ids = ['c1', 'c4', 'c5', 'c2', 'c3']
    _, sidecar = export_subtitles(source.encode(), fmt)
    labels = ('0.1\t0.5\tnew / literal\\t\n1.25\t2\tHello\n2\t3.25\tworld\n'
              f'4.25\t5.25\t[cue:{ids[3]}] plain\n8\t11\tAgain / Later\n')
    operations = [
        {'kind': 'split', 'source': ids[0], 'separator': '\n',
         'targets': [{'line': 2, 'text': 'Hello'}, {'line': 3, 'text': 'world'}]},
        {'kind': 'merge', 'sources': ids[1:3], 'separator': '\n', 'target': 5}]
    return labels, sidecar, operations, ids


class MappingTemplatePolicyTests(unittest.TestCase):
    def test_default_conflicts_and_manual_mapping_control(self):
        labels, sidecar, operations, _ = fixture('srt')
        mapping = {'schema': 'audacity-subtitle-bridge-mapping/1',
                   'sidecar_integrity_sha256': sidecar['integrity_sha256'],
                   'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
                   'operations': operations}
        with self.assertRaisesRegex(BridgeError, 'Unresolved edits') as caught:
            create_mapping_template(labels, sidecar, operations)
        self.assertEqual([c['kind'] for c in caught.exception.report['conflicts']],
                         ['new_label', 'missing_cue'])
        output, report = import_labels(labels, sidecar, mapping=mapping,
                                       missing_policy='keep', new_policy='add')
        self.assertEqual(len(parse_srt(output)[0]), 6)
        self.assertEqual(report['output_count'], 6)

    def test_independent_choices_exact_outputs_and_provenance(self):
        for fmt, missing, new in itertools.product(['srt', 'vtt'], ['keep', 'drop'], ['add', 'ignore']):
            with self.subTest(fmt=fmt, missing=missing, new=new):
                labels, sidecar, operations, ids = fixture(fmt)
                before = copy.deepcopy((sidecar, operations))
                mapping = create_mapping_template(labels, sidecar, operations,
                                                  missing_policy=missing, new_policy=new)
                self.assertEqual(mapping, {
                    'schema': 'audacity-subtitle-bridge-mapping/1',
                    'sidecar_integrity_sha256': sidecar['integrity_sha256'],
                    'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
                    'operations': operations})
                self.assertEqual((sidecar, operations), before)
                with self.assertRaisesRegex(BridgeError, 'Unresolved edits'):
                    import_labels(labels, sidecar, mapping=mapping, output_format=fmt)
                output, report = import_labels(labels, sidecar, mapping=mapping,
                    missing_policy=missing, new_policy=new, output_format=fmt)
                expected = [(1250, 2000, 'Hello'), (2000, 3250, 'world'),
                            (4250, 5250, 'plain')]
                provenance = [(None, 2, [ids[0]]), (None, 3, [ids[0]]), (ids[3], 4, None)]
                if new == 'add':
                    expected.insert(0, (100, 500, 'new / literal\\t'))
                    provenance.insert(0, (None, 1, None))
                if missing == 'keep':
                    expected.append((6000, 7000, 'missing'))
                    provenance.append((ids[4], None, None))
                expected.append((8000, 11000, 'Again\nLater'))
                provenance.append((None, 5, ids[1:3]))
                cues = (parse_srt if fmt == 'srt' else parse_vtt)(output)[0]
                self.assertEqual([(c.start_ms, c.end_ms, c.text) for c in cues], expected)
                self.assertEqual([(c['id'], c['line'], c.get('source_ids'))
                                  for c in report['output_cues']], provenance)
                self.assertIn({'kind': 'new_label', 'line': 1, 'text': 'new / literal\\t',
                               'policy': new}, report['actions'])
                self.assertIn({'kind': 'missing_cue', 'id': ids[4], 'policy': missing}, report['actions'])
                for action in report['actions'][:2]:
                    self.assertEqual(action['sources'], [c for c in sidecar['cues']
                                     if c['id'] in ([ids[0]] if action['kind'] == 'split' else ids[1:3])])
                if fmt == 'vtt':
                    self.assertEqual([c.metadata.get('settings', '') for c in cues],
                        ([''] if new == 'add' else []) + ['align:start', 'align:start', '']
                        + ([''] if missing == 'keep' else []) + ['align:end'])
                    self.assertEqual(cues[-1].source_id, None)
                    self.assertIn('WEBVTT header\n\nNOTE preamble', output)
                    with self.assertRaisesRegex(BridgeError, 'settings'):
                        import_labels(labels, sidecar, mapping=mapping, missing_policy=missing,
                                      new_policy=new, settings_loss_policy='error')

    def test_unresolved_policy_and_ordinary_text_remain_strict(self):
        labels, sidecar, operations, ids = fixture('srt')
        for policies in [dict(missing_policy='keep'), dict(new_policy='add'),
                         dict(missing_policy='keep', new_policy='add')]:
            edited = labels.replace('] plain', '] changed') if len(policies) == 2 else labels
            with self.subTest(policies=policies), self.assertRaisesRegex(BridgeError, 'Unresolved edits') as caught:
                create_mapping_template(edited, sidecar, operations, **policies)
            if hasattr(caught, 'exception'):
                self.assertEqual(caught.exception.report['conflicts'][0]['kind'],
                                 'changed_text' if len(policies) == 2 else
                                 'new_label' if 'missing_policy' in policies else 'missing_cue')

    def test_invalid_policy_values(self):
        labels, sidecar, operations, _ = fixture('srt')
        for key in ['missing_policy', 'new_policy']:
            for value in ['', 'KEEP', 'edited', None, [], {}]:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(BridgeError, 'Unknown .*policy'):
                    create_mapping_template(labels, sidecar, operations, **{key: value})

    def test_permissive_choices_never_bypass_integrity_ids_or_composition(self):
        labels, sidecar, operations, ids = fixture('srt')
        choices = dict(missing_policy='keep', new_policy='add')
        for edited in [labels.replace('[cue:13]', '[cue:unknown]'),
                       labels + '12\t13\t[cue:13] plain\n',
                       labels + '12\t13\t[cue:7] Hello / world\n']:
            with self.subTest(labels=edited), self.assertRaises(BridgeError):
                create_mapping_template(edited, sidecar, operations, **choices)
        for mutate in ['text', 'source', 'target', 'duplicate']:
            invalid = copy.deepcopy(operations)
            if mutate == 'text':
                invalid[0]['targets'][0]['text'] = 'Hell'
            elif mutate == 'source':
                invalid[0]['source'] = 'unknown'
            elif mutate == 'target':
                invalid[1]['target'] = 2
            else:
                invalid.append(invalid[0])
            with self.subTest(mutate=mutate), self.assertRaises(BridgeError):
                create_mapping_template(labels, sidecar, invalid, **choices)
        corrupt = copy.deepcopy(sidecar)
        corrupt['cues'][0]['text'] = 'tampered'
        with self.assertRaisesRegex(BridgeError, 'integrity'):
            create_mapping_template(labels, corrupt, operations, **choices)
        mapping = create_mapping_template(labels, sidecar, operations, **choices)
        with self.assertRaisesRegex(BridgeError, 'labels hash mismatch'):
            import_labels(labels.replace('1.25\t2', '1.3\t2'), sidecar, mapping=mapping, **choices)
        _, other_sidecar = export_subtitles(b'1\n00:00:01,000 --> 00:00:02,000\nother\n', 'srt')
        with self.assertRaisesRegex(BridgeError, 'sidecar hash mismatch'):
            import_labels(labels, other_sidecar, mapping=mapping, **choices)

    def test_cli_choices_dry_run_no_clobber_and_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            labels, sidecar, operations, _ = fixture('srt')
            (work / 'labels.txt').write_text(labels, encoding='utf-8')
            (work / 'sidecar.json').write_text(json.dumps(sidecar), encoding='utf-8')
            (work / 'operations.json').write_text(json.dumps({'operations': operations}), encoding='utf-8')
            output = work / 'mapping.json'
            def run(*extra):
                return subprocess.run([sys.executable, '-m', 'mediawrench.audacity',
                    'mapping-template', str(work / 'labels.txt'), '--sidecar', str(work / 'sidecar.json'),
                    '--operations', str(work / 'operations.json'), '--output', str(output), *extra],
                    cwd=ROOT, capture_output=True, text=True, timeout=120)
            initial_files = sorted(work.iterdir())
            for missing, new in itertools.product(['keep', 'drop'], ['add', 'ignore']):
                flags = ('--missing-policy', missing, '--new-policy', new)
                dry = run(*flags, '--dry-run')
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertEqual(sorted(work.iterdir()), initial_files)
                normal = run(*flags)
                self.assertEqual(normal.returncode, 0, normal.stderr)
                self.assertEqual(normal.stdout, dry.stdout)
                self.assertEqual(output.read_text(), dry.stdout)
                for extra in [(), ('--dry-run',)]:
                    refused = run(*flags, *extra)
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertIn('overwrite', json.loads(refused.stderr)['error'])
                    self.assertEqual(output.read_text(), dry.stdout)
                output.unlink()
            for flags in [(), ('--missing-policy', 'keep'), ('--new-policy', 'add'),
                          ('--missing-policy', 'invalid'), ('--new-policy', 'invalid')]:
                for extra in [(), ('--dry-run',)]:
                    refused = run(*flags, *extra)
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertEqual(refused.stdout, '')
                    if 'invalid' in flags:
                        self.assertIn('invalid choice', refused.stderr)
                    else:
                        self.assertIn('Unresolved edits', json.loads(refused.stderr)['error'])
                    self.assertEqual(sorted(work.iterdir()), initial_files)


if __name__ == '__main__':
    unittest.main()
