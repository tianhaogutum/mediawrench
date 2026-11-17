"""Top-level `mediawrench` dispatcher: overview, version, routing and error paths."""
import subprocess
import sys

import pytest

import mediawrench
from mediawrench import cli

ALL = [command.name for command in cli.COMMANDS]
# Commands whose optional extra is installed; without it, help comes from the dispatcher.
WITH_EXTRA = [c.name for c in cli.COMMANDS if not (c.extra and cli._missing_requirements(c))]


def run(*args, module=("-m", "mediawrench")):
    # Generous bound: cold interpreter starts on shared CI runners (notably macOS) can be slow.
    return subprocess.run([sys.executable, *module, *map(str, args)], capture_output=True, text=True, timeout=120)


def line_for(command):
    marker = "*" if command.binaries else " "
    return f"\n {marker}{command.name} "


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"mediawrench {mediawrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for command in cli.COMMANDS:
        assert line_for(command) in out
    assert "(coming soon)" not in out
    for command in cli.COMMANDS:
        if command.extra and cli._missing_requirements(command):
            assert f"(needs mediawrench[{command.extra}])" in out


def test_command_table_shape():
    assert [c.name for c in cli.COMMANDS if c.group == "Subtitles & captions"] == [
        "rollup", "reflow", "audacity", "cutsync"]
    assert [c.name for c in cli.COMMANDS if c.group == "Transcripts & speech"] == [
        "asr-audit", "speakers", "cutlist", "chunks"]
    assert [c.name for c in cli.COMMANDS if c.group == "Audio & video files"] == [
        "qc", "loudness", "postflight", "chapters", "drift"]
    assert [c.name for c in cli.COMMANDS if c.group == "Photos"] == ["cull"]
    assert {c.name: c.module for c in cli.COMMANDS} == {
        "rollup": "mediawrench.rollup.cli", "reflow": "mediawrench.reflow.cli",
        "audacity": "mediawrench.audacity.cli", "asr-audit": "mediawrench.asraudit.cli",
        "speakers": "mediawrench.speakers.cli", "cutlist": "mediawrench.cutlist.cli",
        "chunks": "mediawrench.chunks.cli", "qc": "mediawrench.qc.cli", "loudness": "mediawrench.loudness.cli",
        "postflight": "mediawrench.postflight.cli", "chapters": "mediawrench.chapters.cli",
        "cutsync": "mediawrench.cutsync.cli", "drift": "mediawrench.drift.cli", "cull": "mediawrench.cull.cli",
    }
    assert {c.name: c.binaries for c in cli.COMMANDS if c.binaries} == {
        "cutlist": ("ffprobe",), "qc": ("ffmpeg", "ffprobe"), "loudness": ("ffmpeg", "ffprobe"),
        "postflight": ("ffmpeg", "ffprobe"), "chapters": ("ffprobe",), "drift": ("ffmpeg", "ffprobe")}
    assert {c.name: (c.extra, c.requires) for c in cli.COMMANDS if c.extra or c.requires} == {
        "drift": ("drift", ("numpy", "scipy", "soxr", "soundfile")), "cull": ("cull", ("lxml", "simplejson"))}
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)
    # qc is new in mediawrench; every other command replaces a former standalone tool.
    assert set(cli.LEGACY.values()) == set(cli.BY_NAME) - {"qc"}


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"mediawrench {mediawrench.__version__}\n"


@pytest.mark.parametrize("tool", WITH_EXTRA)
def test_tool_help_uses_new_program_name(tool):
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    assert f"usage: mediawrench {tool}" in result.stdout


@pytest.mark.parametrize("tool", WITH_EXTRA)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["help", tool])
    assert exc.value.code == 0
    assert f"usage: mediawrench {tool}" in capsys.readouterr().out


@pytest.mark.parametrize("tool", ["rollup", "reflow", "asr-audit", "chunks", "audacity", "qc",
                                  *(["drift"] if "drift" in WITH_EXTRA else [])])
def test_tool_version_flags_use_new_program_name(tool):
    result = run(tool, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"mediawrench {tool} {mediawrench.__version__}\n"


def test_tool_exit_codes_pass_through():
    bad = run("chapters", "no-such-subcommand")
    assert bad.returncode == 2 and "usage: mediawrench chapters" in bad.stderr
    missing = run("rollup", "does-not-exist.vtt", "--dry-run")
    assert missing.returncode == 2 and missing.stderr.startswith("mediawrench rollup: ")


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["chapter"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'chapter'" in err and "did you mean 'chapters'?" in err


@pytest.mark.parametrize("old,new", sorted(cli.LEGACY.items()))
def test_unknown_legacy_name_points_to_new_command(old, new, capsys):
    assert cli.main([old]) == 2
    assert f"{old} is now 'mediawrench {new}'" in capsys.readouterr().err


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "mediawrench.ghost_tool.cli", "not shipped", "Audio & video files")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    assert "ghost" in cli.overview() and "(coming soon)" in cli.overview()
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("mediawrench: command 'ghost' is not available") and "Traceback" not in err


@pytest.mark.parametrize("name", ["drift", "cull"])
def test_shipped_extra_command_without_its_dependencies_prints_install_hint(name, monkeypatch, capsys):
    real = cli._present
    command = cli.BY_NAME[name]
    monkeypatch.setattr(cli, "_present", lambda module: False if module in command.requires else real(module))
    assert f"(needs mediawrench[{name}])" in cli.overview()
    hint = (f"mediawrench {name}: missing optional dependencies; "
            f"install them with: pip install 'mediawrench[{name}]'\n")
    assert cli.main([name, "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == hint
    assert cli.main([name, "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: mediawrench {name} <subcommand>") and out.endswith(hint)


def test_command_with_missing_extra_prints_install_hint(monkeypatch, capsys):
    # Exercise the dispatcher path with a stand-in whose requirement can never be installed.
    fake = cli.Command("fake", "mediawrench.rollup.cli", "stand-in", "Subtitles & captions",
                       extra="fake", requires=("mediawrench_no_such_module",))
    monkeypatch.setitem(cli.BY_NAME, "fake", fake)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, fake))
    assert "(needs mediawrench[fake])" in cli.overview()
    hint = "mediawrench fake: missing optional dependencies; install them with: pip install 'mediawrench[fake]'\n"
    assert cli.main(["fake", "input.vtt"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == hint
    assert cli.main(["fake", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: mediawrench fake <subcommand>") and out.endswith(hint)


@pytest.mark.parametrize("module", ["mediawrench.rollup", "mediawrench.reflow", "mediawrench.audacity",
                                    "mediawrench.asraudit", "mediawrench.speakers", "mediawrench.cutlist",
                                    "mediawrench.chunks", "mediawrench.qc", "mediawrench.loudness",
                                    "mediawrench.postflight", "mediawrench.chapters", "mediawrench.cutsync",
                                    "mediawrench.drift", "mediawrench.cull"])
def test_subpackage_main_routes_through_dispatcher(module):
    result = run("--help", module=("-m", module))
    assert result.returncode == 0, result.stderr
    assert "usage: mediawrench " in result.stdout


# Every command and its subcommands (`cutlist` dispatches its two verify commands by hand).
SUBCOMMANDS = {
    "rollup": [],
    "reflow": [],
    "audacity": ["export", "mapping-template", "import"],
    "asr-audit": [],
    "speakers": [],
    "cutlist": ["verify-companions", "verify-probe-evidence"],
    "chunks": ["split", "stitch"],
    "qc": [],
    "loudness": [],
    "postflight": [],
    "chapters": ["convert", "inspect", "compare", "verify-media", "check-inputs", "restore-source", "check-export"],
    "cutsync": [],
    "drift": ["analyze", "anchor", "inspect", "clips", "fit", "render"],
    "cull": ["plan", "merge", "verify"],
}
# Subcommand help needs the real parser, so commands whose extra is missing are left out here
# (their dispatcher help is covered by test_shipped_extra_command_without_its_dependencies_...).
ALL_HELP = [(name, None) for name in SUBCOMMANDS] + [
    (name, sub) for name, subs in SUBCOMMANDS.items() if name in WITH_EXTRA for sub in subs]


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_subcommand_table_covers_every_command():
    assert set(SUBCOMMANDS) == set(cli.BY_NAME)


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("command,sub", ALL_HELP, ids=lambda v: v or "-")
def test_every_command_and_subcommand_help_exits_0(command, sub, flag, capsys):
    argv = [command, *([sub] if sub else []), flag]
    code, out, err = _main(argv, capsys)
    assert code == 0, (argv, err)
    assert "usage" in out.lower(), (argv, out)
    assert f"mediawrench {command}" in out, (argv, out)
    if sub:
        assert f"mediawrench {command} {sub}" in out, (argv, out)
