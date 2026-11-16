"""Normalization alias safety: bounded, zero-tool input-error regressions."""
from contextlib import contextmanager, ExitStack, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from mediawrench.loudness import Profile, Toolchain, normalize
from mediawrench.loudness.cli import main
from mediawrench.loudness.core import AudioError, DeadlineExceeded, JobCancelled


class NormalizePreflightTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.source = self.root / 'source.wav'
        self.source.write_bytes(b'unchanged source')
        self.source.chmod(0o444)
        self.output = self.root / 'output.wav'
        self.report = self.root / 'report.json'
        self.profile = Profile(-16, -1.5, 0.5)
        self.loop = self.root / 'self-loop'
        self.loop.symlink_to(self.loop.name)
        self.cycle = self.root / 'cycle-a'
        other = self.root / 'cycle-b'
        self.cycle.symlink_to(other.name)
        other.symlink_to(self.cycle.name)

    def snapshot(self):
        return {p.name: (p.lstat().st_mode, os.readlink(p) if p.is_symlink() else p.read_bytes())
                for p in self.root.iterdir()}

    @contextmanager
    def no_processing(self, no_writes=False):
        with ExitStack() as stack:
            for name in ('Runner.run', 'subprocess.Popen', 'tempfile.TemporaryDirectory',
                         '_copy_source', 'sha256'):
                mock = stack.enter_context(patch('mediawrench.loudness.core.' + name,
                                                 side_effect=AssertionError('must not process')))
                stack.callback(mock.assert_not_called)
            if no_writes:
                for name in ('builtins.open', 'io.open', 'os.open',
                             'mediawrench.loudness.core.tempfile.mkstemp',
                             'mediawrench.loudness.core._write_report'):
                    mock = stack.enter_context(patch(name, side_effect=AssertionError('must not open/write')))
                    stack.callback(mock.assert_not_called)
                for name in ('mkdir', 'unlink', 'remove', 'rmdir', 'rename', 'replace', 'link', 'chmod'):
                    mock = stack.enter_context(patch('os.' + name, side_effect=AssertionError('must not mutate')))
                    stack.callback(mock.assert_not_called)
            yield

    def call(self, paths, cli=False, **kwargs):
        if not cli:
            return normalize(*paths[:2], kwargs.pop('profile', self.profile), paths[2], **kwargs)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(paths[0]), str(paths[1]), '--report', str(paths[2]),
                         '--target-i', '-16', '--target-tp', '-1.5', '--tolerance', '0.5'])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, {'PASS': 0, 'FAIL': 2, 'UNSUPPORTED': 3, 'ERROR': 4}[result['status']])
        return result

    def assert_error(self, result, diagnostic=None, unsafe=False):
        self.assertEqual(result['status'], 'ERROR', result)
        self.assertFalse(result['published'])
        self.assertEqual(result['commands'], [])
        self.assertEqual(result['failed_stage'], 'preparation')
        self.assertTrue(result['error'])
        if diagnostic:
            self.assertIn(diagnostic, result['error'])
        self.assertEqual('report_error' in result, unsafe)
        if not unsafe:
            self.assertNotIn('report_errno', result)
        self.assertFalse(set(result) & {'input_media', 'output_media', 'checks', 'source_sha256'})

    def test_symlink_loops_in_each_path_and_parent_before_tools_or_writes(self):
        before = self.snapshot()
        for loop in (self.loop, self.cycle):
            for path in (loop, loop / 'child.wav'):
                for position in range(3):
                    paths = [self.source, self.output, self.report]
                    paths[position] = path
                    for cli in (False, True):
                        with self.subTest(loop=loop.name, parent=path != loop, position=position, cli=cli):
                            with self.no_processing(no_writes=True):
                                result = self.call(paths, cli)
                            self.assert_error(result, unsafe=True)
                            self.assertEqual(result.get('errno'), errno.ELOOP)
                            self.assertNotIn('error_kind', result)
                            self.assertEqual(self.snapshot(), before)

    def test_loop_never_writes_aliased_or_existing_report_or_output(self):
        existing = self.root / 'existing'
        existing.write_bytes(b'keep destination')
        alias = self.root / 'source-alias'
        alias.symlink_to(self.source.name)
        for output, report in ((self.output, self.source), (self.output, alias),
                               (self.output, existing), (self.output, self.output),
                               (existing, self.report), (self.loop, self.loop)):
            before = self.snapshot()
            for cli in (False, True):
                with self.subTest(output=output, report=report, cli=cli), self.no_processing(no_writes=True):
                    result = self.call([self.loop, output, report], cli)
                self.assert_error(result, unsafe=True)
                self.assertNotIn('report_errno', result)
                self.assertEqual(result.get('errno'), errno.ELOOP)
                self.assertEqual(self.snapshot(), before)

    def test_missing_and_dangling_source_retain_safe_failure_report(self):
        missing = self.root / 'missing.wav'
        dangling = self.root / 'dangling.wav'
        dangling.symlink_to(missing.name)
        for source in (missing, dangling):
            with self.subTest(source=source), self.no_processing():
                result = self.call([source, self.output, self.report])
            self.assert_error(result, 'existing local regular file')
            self.assertNotIn('errno', result)
            self.assertEqual(json.loads(self.report.read_text()), result)
            self.report.unlink()
            self.assertFalse(self.output.exists())

    def test_aliases_and_existing_destinations_preserve_bytes(self):
        alias = self.root / 'source-alias.wav'
        alias.symlink_to(self.source.name)
        dangling = self.root / 'dangling-report'
        dangling.symlink_to('missing.json')
        existing = self.root / 'existing.wav'
        existing.write_bytes(b'keep destination')
        cases = [(self.output, self.source, 'distinct', True),
                 (self.output, alias, 'distinct', True),
                 (self.output, self.output, 'distinct', True),
                 (self.output, existing, 'Report already exists', True),
                 (self.output, dangling, 'Report already exists', True),
                 (alias, self.report, 'distinct', False),
                 (existing, self.report, 'Output already exists', False)]
        for output, report, diagnostic, unsafe in cases:
            before = self.snapshot()
            with self.subTest(output=output, report=report), self.no_processing(no_writes=unsafe):
                result = self.call([self.source, output, report])
            self.assert_error(result, diagnostic, unsafe)
            self.assertNotIn('report_errno', result)
            self.assertNotIn('errno', result)
            if not unsafe:
                self.assertEqual(json.loads(self.report.read_text()), result)
                self.report.unlink()
            self.assertEqual(self.snapshot(), before)

    def test_path_safety_precedes_tokens_and_settings(self):
        stop = Event()
        stop.set()
        options = [dict(cancel_event=object()), dict(cancel_event=stop),
                   dict(profile=Profile(-4, -1.5, 0.5)), dict(audio_stream=-1),
                   dict(tools=Toolchain(timeout=0)), dict(tools=Toolchain(total_timeout=0))]
        for position in range(3):
            paths = [self.source, self.output, self.report]
            paths[position] = self.loop
            for kwargs in options:
                with self.subTest(position=position, options=kwargs), self.no_processing(no_writes=True):
                    result = self.call(paths, **kwargs)
                self.assert_error(result, 'loop', unsafe=True)
                self.assertEqual(result.get('errno'), errno.ELOOP)
                self.assertNotIn('error_kind', result)
        self.assertTrue(stop.is_set())

    def test_safe_paths_preserve_event_and_settings_precedence(self):
        stop = Event()
        stop.set()
        cases = [(dict(cancel_event=object(), profile=Profile(-4, -1.5, 0.5)), 'threading.Event'),
                 (dict(cancel_event=stop, profile=Profile(-4, -1.5, 0.5)), 'Job cancelled'),
                 (dict(profile=Profile(-4, -1.5, 0.5), audio_stream=-1), 'target_i'),
                 (dict(audio_stream=-1, tools=Toolchain(timeout=0)), 'nonnegative integer'),
                 (dict(cancel_event=Event(), tools=Toolchain(timeout=0)), 'timeout'),
                 (dict(tools=Toolchain(total_timeout=0)), 'total_timeout')]
        for kwargs, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.no_processing():
                result = self.call([self.source, self.output, self.report], **kwargs)
            self.assert_error(result, diagnostic)
            self.assertNotIn('errno', result)
            self.assertEqual(json.loads(self.report.read_text()), result)
            self.report.unlink()
        self.assertTrue(stop.is_set())

    def test_native_oserror_errno_preserved_with_safe_or_unsafe_report(self):
        # Inject permission failures, independent of the runner's OS privileges.
        for stage, unsafe in (('mediawrench.loudness.core._resolve_alias', True),
                              ('pathlib.Path.is_file', False)):
            for failure in (PermissionError(errno.EACCES, 'native permission denied', str(self.source)),
                            PermissionError(errno.EPERM, 'native operation denied', str(self.source)),
                            FileNotFoundError(errno.ENOENT, 'native missing path', str(self.source)),
                            OSError('no native code')):
                for cli in (False, True):
                    before = self.snapshot()
                    with self.subTest(stage=stage, failure=failure, cli=cli), \
                         self.no_processing(no_writes=unsafe), patch(stage, side_effect=failure):
                        result = self.call([self.source, self.output, self.report], cli)
                    self.assert_error(result, unsafe=unsafe)
                    self.assertIn('errno', result)
                    self.assertEqual(result['errno'], failure.errno)
                    self.assertEqual(result['error'], str(failure))
                    self.assertNotIn('error_kind', result)
                    if not unsafe:
                        self.assertEqual(json.loads(self.report.read_text()), result)
                        self.report.unlink()
                    self.assertEqual(self.snapshot(), before)

    def test_non_oserror_categories_do_not_inherit_errno_from_cause(self):
        cases = [(AudioError('ordinary validation'), 'ERROR', None),
                 (AudioError('unsupported media', 'UNSUPPORTED'), 'UNSUPPORTED', None),
                 (AudioError('gate failed', 'FAIL'), 'FAIL', None),
                 (ValueError('invalid value'), 'ERROR', None),
                 (JobCancelled('Job cancelled'), 'ERROR', 'cancelled'),
                 (DeadlineExceeded('Job deadline exceeded'), 'ERROR', 'deadline_exceeded'),
                 (KeyboardInterrupt(), 'ERROR', 'cancelled')]
        for failure, status, kind in cases:
            failure.__cause__ = PermissionError(errno.EACCES, 'underlying error')
            with self.subTest(failure=type(failure).__name__, status=status), self.no_processing(), \
                 patch('mediawrench.loudness.core.Profile.validate', side_effect=failure):
                result = self.call([self.source, self.output, self.report])
            self.assertEqual(result['status'], status)
            self.assertFalse(result['published'])
            self.assertEqual(result['commands'], [])
            self.assertEqual(result.get('error_kind'), kind)
            self.assertNotIn('errno', result)
            self.assertEqual(result['error'], 'Job cancelled' if isinstance(failure, KeyboardInterrupt)
                             else str(failure))
            self.assertEqual(json.loads(self.report.read_text()), result)
            self.report.unlink()

    def test_secondary_report_errno_preserves_primary_diagnostic_and_exit(self):
        primaries = [PermissionError(errno.EACCES, 'source denied'), OSError('no primary code'),
                     AudioError('invalid source'), AudioError('unsupported', 'UNSUPPORTED'),
                     AudioError('gate failed', 'FAIL'), ValueError('invalid value'),
                     JobCancelled('Job cancelled'), DeadlineExceeded('Job deadline exceeded'),
                     KeyboardInterrupt()]
        secondaries = [PermissionError(errno.EACCES, 'report permission denied'),
                       PermissionError(errno.EPERM, 'report operation denied'),
                       OSError(errno.ENOSPC, 'report storage full'), OSError('no report code'),
                       ValueError('invalid report')]
        secondaries[-1].__cause__ = PermissionError(errno.EACCES, 'underlying error')
        for failure in primaries:
            for secondary in secondaries:
                for cli in (False, True):
                    before = self.snapshot()
                    with self.subTest(primary=type(failure).__name__, secondary=secondary, cli=cli), \
                         self.no_processing(), \
                         patch('mediawrench.loudness.core.Profile.validate', side_effect=failure), \
                         patch('mediawrench.loudness.core._write_report', side_effect=secondary) as delivery:
                        result = self.call([self.source, self.output, self.report], cli)
                    delivery.assert_called_once_with(self.report, result)
                    status = failure.status if isinstance(failure, AudioError) else 'ERROR'
                    self.assertEqual(result['status'], status)
                    self.assertFalse(result['published'])
                    self.assertEqual(result['commands'], [])
                    self.assertEqual(result['failed_stage'], 'preparation')
                    kind = ('deadline_exceeded' if isinstance(failure, DeadlineExceeded) else
                            'cancelled' if isinstance(failure, (JobCancelled, KeyboardInterrupt)) else None)
                    self.assertEqual(result.get('error_kind'), kind)
                    self.assertEqual(result['error'], 'Job cancelled' if isinstance(failure, KeyboardInterrupt)
                                     else str(failure))
                    self.assertEqual(result['report_error'], str(secondary))
                    if isinstance(failure, OSError):
                        self.assertIn('errno', result)
                        self.assertEqual(result['errno'], failure.errno)
                    else:
                        self.assertNotIn('errno', result)
                    if isinstance(secondary, OSError):
                        self.assertIn('report_errno', result)
                        self.assertEqual(result['report_errno'], secondary.errno)
                    else:
                        self.assertNotIn('report_errno', result)
                    self.assertEqual(self.snapshot(), before)

    def test_native_report_link_failure_cleans_temporary_report(self):
        # Exercise real report creation and cleanup, injecting only its publication.
        for code in (errno.EACCES, errno.EPERM, errno.ENOSPC, None):
            secondary = OSError(code, 'report publication failed') if code else OSError('no report code')
            before = self.snapshot()
            with self.subTest(code=code), self.no_processing(), \
                 patch('mediawrench.loudness.core.Profile.validate', side_effect=AudioError('invalid source')), \
                 patch('mediawrench.loudness.core.os.link', side_effect=secondary) as link:
                result = self.call([self.source, self.output, self.report])
            link.assert_called_once()
            self.assertEqual(result['error'], 'invalid source')
            self.assertNotIn('errno', result)
            self.assertEqual(result['report_error'], str(secondary))
            self.assertIn('report_errno', result)
            self.assertEqual(result['report_errno'], code)
            self.assertEqual(self.snapshot(), before)

    def test_unexpected_report_runtime_error_propagates(self):
        for stage in ('mediawrench.loudness.core._write_report', 'mediawrench.loudness.core.os.link'):
            failure = RuntimeError('report programming error')
            before = self.snapshot()
            with self.subTest(stage=stage), self.no_processing(), \
                 patch('mediawrench.loudness.core.Profile.validate', side_effect=AudioError('invalid source')), \
                 patch(stage, side_effect=failure), self.assertRaises(RuntimeError) as caught:
                self.call([self.source, self.output, self.report])
            self.assertIs(caught.exception, failure)
            self.assertEqual(self.snapshot(), before)

    def test_unrelated_runtime_errors_propagate_without_failure_report(self):
        for stage in ('os.path.realpath', 'mediawrench.loudness.core.Profile.validate',
                      'mediawrench.loudness.core.Toolchain.validate', 'mediawrench.loudness.core.Runner.run',
                      'mediawrench.loudness.cli.normalize'):
            for cli in ((True,) if stage.endswith('cli.normalize') else (False, True)):
                failure = RuntimeError('unrelated programming error')
                before = self.snapshot()
                with self.subTest(stage=stage, cli=cli), self.no_processing(no_writes=True), \
                     patch(stage, side_effect=failure), self.assertRaises(RuntimeError) as caught:
                    self.call([self.source, self.output, self.report], cli)
                self.assertIs(caught.exception, failure)
                self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
