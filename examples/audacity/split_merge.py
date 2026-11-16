"""Declare operations, generate/review a mapping template, then import real files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SOURCE = ('7\n00:00:01,000 --> 00:00:03,000\nHello\nworld\n\n'
          '9\n00:00:04,000 --> 00:00:05,000\nAgain\n\n'
          '11\n00:00:06,000 --> 00:00:07,000\nLater\n\n')
EDITED = '1\t2\tHello\n2\t3\tworld\n4\t7\tAgain / Later\n'
EXPECTED = ('1\n00:00:01,000 --> 00:00:02,000\nHello\n\n'
            '2\n00:00:02,000 --> 00:00:03,000\nworld\n\n'
            '3\n00:00:04,000 --> 00:00:07,000\nAgain\nLater\n\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console', type=Path, help='run this mediawrench executable instead of python -m')
    args = parser.parse_args()
    command = ([str(args.console.resolve()), 'audacity'] if args.console
               else [sys.executable, '-m', 'mediawrench.audacity'])
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(filter(None, [str(root / 'src'), os.environ.get('PYTHONPATH')]))}
    with tempfile.TemporaryDirectory(prefix='asb-mapping-') as directory:
        work = Path(directory)

        def run(*arguments, expected=0):
            result = subprocess.run(command + list(map(str, arguments)), cwd=work if args.console else root,
                                    env=env, capture_output=True, text=True, timeout=120)
            if result.returncode != expected:
                raise RuntimeError(result.stderr)
            return json.loads(result.stdout if expected == 0 else result.stderr)

        source, labels, sidecar, plan, mapping, output = [work / name for name in
            ['source.srt', 'labels.txt', 'sidecar.json', 'operations.json', 'mapping.json', 'out.srt']]
        source.write_text(SOURCE, encoding='utf-8')
        run('export', source, '--labels', labels, '--sidecar', sidecar)
        labels.write_text(EDITED, encoding='utf-8')
        operations = [
                     {'kind': 'split', 'source': '7', 'separator': '\n',
                      'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]},
                     {'kind': 'merge', 'sources': ['9', '11'], 'separator': '\n', 'target': 3}]
        plan.write_text(json.dumps({'operations': operations}), encoding='utf-8')
        dry = run('mapping-template', labels, '--sidecar', sidecar, '--operations', plan,
                   '--output', mapping, '--dry-run')
        if mapping.exists():
            raise RuntimeError('Template dry-run wrote a mapping')
        rules = run('mapping-template', labels, '--sidecar', sidecar, '--operations', plan,
                     '--output', mapping)
        expected_rules = {'schema': 'audacity-subtitle-bridge-mapping/1',
                          'sidecar_integrity_sha256': json.loads(sidecar.read_text(encoding='utf-8'))['integrity_sha256'],
                          'labels_sha256': hashlib.sha256(EDITED.encode('utf-8')).hexdigest(),
                          'operations': operations}
        if rules != expected_rules or rules != dry or json.loads(mapping.read_text(encoding='utf-8')) != rules:
            raise RuntimeError('Template differs from caller operations and expected input hashes')
        report = run('import', labels, '--sidecar', sidecar, '--mapping', mapping,
                     '--source', source, '--output', output)
        if output.read_text(encoding='utf-8') != EXPECTED:
            raise RuntimeError('Split/merge output differs from the explicit expected SRT')
        if [c['source_ids'] for c in report['output_cues']] != [['7'], ['7'], ['9', '11']]:
            raise RuntimeError('Missing source provenance')
        labels.write_text(EDITED.replace('1\t2', '1.1\t2'), encoding='utf-8')
        stale_output = work / 'stale.srt'
        stale = run('import', labels, '--sidecar', sidecar, '--mapping', mapping,
                    '--output', stale_output, expected=2)
        if ('labels hash mismatch' not in stale['error'] or stale_output.exists()
                or Path(str(stale_output) + '.report.json').exists()):
            raise RuntimeError('Stale template mapping did not fail without outputs')
        labels.write_text(EDITED, encoding='utf-8')
        operations[0]['targets'][1]['text'] = 'worl'
        plan.write_text(json.dumps({'operations': operations}), encoding='utf-8')
        invalid_mapping = work / 'invalid-mapping.json'
        invalid = run('mapping-template', labels, '--sidecar', sidecar, '--operations', plan,
                      '--output', invalid_mapping, expected=2)
        if 'reconstruct' not in invalid['error'] or invalid_mapping.exists():
            raise RuntimeError('Invalid template composition did not fail without output')
        rules['operations'][0]['targets'][1]['text'] = 'worl'
        mapping.write_text(json.dumps(rules), encoding='utf-8')
        rejected = work / 'rejected.srt'
        failure = run('import', labels, '--sidecar', sidecar, '--mapping', mapping,
                      '--output', rejected, '--text-policy', 'edited', expected=2)
        if ('reconstruct' not in failure['error'] or rejected.exists()
                or Path(str(rejected) + '.report.json').exists()):
            raise RuntimeError('Invalid split did not fail without outputs')
        print(json.dumps({'split_source': '7', 'merged_sources': ['9', '11'],
                          'output_cues': report['output_count'], 'exact_expected_srt': True,
                          'template_matches_caller_operations_and_hashes': True,
                          'template_dry_run_wrote_no_files': True,
                          'stale_mapping_exit': 2, 'invalid_template_exit': 2,
                          'text_loss_exit': 2, 'failed_import_wrote_no_outputs': True}, indent=2))


if __name__ == '__main__':
    main()
