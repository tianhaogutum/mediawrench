"""Listing diagnostics: preserve caught native errno without changing path policy."""
from contextlib import contextmanager, ExitStack, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from mediawrench.loudness import list_streams
from mediawrench.loudness.cli import main
from mediawrench.loudness.core import AudioError, DeadlineExceeded, JobCancelled


class ListStreamsErrorsTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.source = self.root / 'source.mkv'
        self.source.write_bytes(b'metadata-only tests; probe is injected')

    @contextmanager
    def metadata_only(self, launch_error=None):
        with ExitStack() as stack:
            for name in ('builtins.open', 'io.open', 'os.open'):
                stack.enter_context(patch(name, side_effect=AssertionError('must not open source')))
            for name in ('mkdir', 'remove', 'unlink', 'rmdir', 'rename', 'replace', 'link', 'symlink', 'chmod'):
                stack.enter_context(patch('os.' + name, side_effect=AssertionError('must not mutate files')))
            for name in ('TemporaryDirectory', 'mkstemp'):
                stack.enter_context(patch('mediawrench.loudness.core.tempfile.' + name,
                                          side_effect=AssertionError('must not stage files')))
            spawn = stack.enter_context(patch('mediawrench.loudness.core.subprocess.Popen',
                                              side_effect=launch_error or AssertionError('must not start tools')))
            yield spawn
            if launch_error is None:
                spawn.assert_not_called()

    def api_cli(self, source=None, **kwargs):
        source = self.source if source is None else source
        api = list_streams(source, **kwargs)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(source), '--list-streams'])
        self.assertEqual(json.loads(stdout.getvalue()), api)
        self.assertEqual(code, 3 if api['status'] == 'UNSUPPORTED' else 4)
        return api

    def expected(self, error, status='ERROR', **fields):
        return dict(schema_version=1, status=status, source=str(self.source),
                    published=False, commands=[], error=error, **fields)

    def test_native_metadata_errno_api_cli_before_tools_or_open(self):
        for failure in (PermissionError(errno.EACCES, 'native permission denied', str(self.source)),
                        PermissionError(errno.EPERM, 'native operation denied', str(self.source)),
                        OSError('native error without code')):
            for token in (None, Event()):
                with self.subTest(errno=failure.errno, token=token), self.metadata_only(), \
                     patch.object(Path, 'stat', side_effect=failure):
                    result = self.api_cli(cancel_event=token)
                    self.assertEqual(result, self.expected(str(failure), errno=failure.errno))

    def test_non_oserror_categories_never_infer_errno(self):
        for failure, status, kind in (
                (AudioError('invalid input'), 'ERROR', None),
                (AudioError('unsupported media', 'UNSUPPORTED'), 'UNSUPPORTED', None),
                (JobCancelled('Job cancelled'), 'ERROR', 'cancelled'),
                (KeyboardInterrupt(), 'ERROR', 'cancelled'),
                (DeadlineExceeded('budget spent'), 'ERROR', 'deadline_exceeded')):
            failure.__cause__ = PermissionError(errno.EACCES, 'underlying error')
            failure.errno = errno.EPERM
            with self.subTest(failure=type(failure).__name__), self.metadata_only(), \
                 patch.object(Path, 'stat', side_effect=failure):
                result = self.api_cli()
            fields = {'error_kind': kind} if kind else {}
            error = 'Job cancelled' if isinstance(failure, KeyboardInterrupt) else str(failure)
            self.assertEqual(result, self.expected(error, status, **fields))

    def test_tool_launch_oserror_remains_wrapped_without_errno(self):
        for failure, prefix in ((FileNotFoundError(errno.ENOENT, 'not installed'), 'Executable not found:'),
                                (PermissionError(errno.EACCES, 'not executable'), 'Cannot execute'),
                                (OSError('launch without code'), 'Cannot execute')):
            with self.subTest(failure=failure), self.metadata_only(launch_error=failure) as spawn:
                result = self.api_cli()
            self.assertEqual(spawn.call_count, 2)
            expected = self.expected(result['error'])
            expected['commands'] = [spawn.call_args.args[0]]
            self.assertEqual(result, expected)
            self.assertTrue(result['error'].startswith(prefix), result)

    def test_missing_dangling_loops_and_nonregular_keep_validation_policy(self):
        missing = self.root / 'missing.mkv'
        dangling = self.root / 'dangling.mkv'
        dangling.symlink_to(missing)
        loop = self.root / 'loop.mkv'
        loop.symlink_to(loop.name)
        cycle = self.root / 'cycle-a.mkv'
        other = self.root / 'cycle-b.mkv'
        cycle.symlink_to(other.name)
        other.symlink_to(cycle.name)
        paths = [missing, dangling, loop, cycle, self.root]
        directory_link = self.root / 'directory-link'
        directory_link.symlink_to(self.root, target_is_directory=True)
        paths.append(directory_link)
        if hasattr(os, 'mkfifo'):
            fifo = self.root / 'no-writer.mkv'
            os.mkfifo(fifo)
            fifo_link = self.root / 'fifo-link'
            fifo_link.symlink_to(fifo)
            paths.extend((fifo, fifo_link))
        for path in paths:
            with self.subTest(path=path), self.metadata_only():
                result = self.api_cli(path)
            expected = self.expected('Input must be an existing local regular file')
            expected['source'] = str(path)
            self.assertEqual(result, expected)

    def test_regular_file_symlink_keeps_original_path_and_inventory(self):
        link = self.root / 'alias.mkv'
        link.symlink_to(self.source)
        info = dict(format=dict(format_name='matroska,webm'), streams=[])
        raw = subprocess.CompletedProcess([], 0, json.dumps(info), '')
        with self.metadata_only(), patch('mediawrench.loudness.core.Runner.run', return_value=raw) as run:
            result = list_streams(link)
        self.assertEqual(result, dict(schema_version=1, status='OK', source=str(link),
                                      published=False, commands=[], format='matroska,webm', streams=[]))
        self.assertEqual(run.call_args.args[0][-1], str(link))

    def test_unexpected_runtime_and_existing_value_error_categories(self):
        for stage in ('pathlib.Path.stat', 'mediawrench.loudness.core.Runner.run',
                      'mediawrench.loudness.core.json.loads'):
            failure = RuntimeError('unexpected programming error')
            for call in ('api', 'cli'):
                with self.subTest(stage=stage, call=call), self.metadata_only(), ExitStack() as stack:
                    if stage.endswith('json.loads'):
                        stack.enter_context(patch('mediawrench.loudness.core.Runner.run',
                                                  return_value=subprocess.CompletedProcess([], 0, '{}', '')))
                    stack.enter_context(patch(stage, side_effect=failure))
                    stdout = io.StringIO()
                    with redirect_stdout(stdout), self.assertRaises(RuntimeError) as caught:
                        if call == 'api':
                            list_streams(self.source)
                        else:
                            main([str(self.source), '--list-streams'])
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(stdout.getvalue(), '')
        failure = ValueError('unexpected metadata value error')
        failure.__cause__ = PermissionError(errno.EACCES, 'underlying error')
        failure.errno = errno.EPERM
        with self.metadata_only(), patch.object(Path, 'stat', side_effect=failure):
            # Path.is_file already turns ValueError (e.g. an embedded NUL) into False.
            self.assertEqual(self.api_cli(), self.expected('Input must be an existing local regular file'))
        with self.metadata_only(), patch('mediawrench.loudness.core.Runner.run', side_effect=failure):
            for call in (lambda: list_streams(self.source),
                         lambda: main([str(self.source), '--list-streams'])):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertIs(caught.exception, failure)


if __name__ == '__main__':
    unittest.main()
