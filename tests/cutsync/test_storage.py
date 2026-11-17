import os

import pytest

from mediawrench.cutsync import CutSyncError
from mediawrench.cutsync.storage import write_outputs


@pytest.mark.parametrize("force", [True, False])
def test_second_publish_failure_rolls_back_pair(tmp_path, monkeypatch, force):
    first, second = tmp_path / "output.srt", tmp_path / "report.json"
    if force:
        first.write_text("original srt")
        second.write_text("original report")
    real_link, real_replace = os.link, os.replace

    def fail_link(source, destination, *args, **kwargs):
        if destination == second and str(source).endswith(".tmp"):
            raise OSError("injected second-file link failure")
        return real_link(source, destination, *args, **kwargs)

    def fail_replace(source, destination, *args, **kwargs):
        if destination == second and str(source).endswith(".tmp"):
            raise OSError("injected second-file replace failure")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_link)
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_outputs([(first, "new srt"), (second, "new report")], [], overwrite=force)
    if force:
        assert first.read_text() == "original srt"
        assert second.read_text() == "original report"
    else:
        assert not first.exists()
        assert not second.exists()
    assert not list(tmp_path.glob(".*.cut-sync-*"))


def test_force_success_and_no_temp_files(tmp_path):
    first, second = tmp_path / "out.srt", tmp_path / "out.json"
    first.write_text("old")
    second.write_text("old")
    write_outputs([(first, "new"), (second, "report")], [], overwrite=True)
    assert first.read_text() == "new"
    assert second.read_text() == "report"
    assert len(list(tmp_path.iterdir())) == 2


def test_symlink_output_refused_even_when_broken(tmp_path):
    link = tmp_path / "output.srt"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(CutSyncError, match="symlink"):
        write_outputs([(link, "text")], [], overwrite=True)


def test_duplicate_destination_refused(tmp_path):
    target = tmp_path / "output.srt"
    with pytest.raises(CutSyncError, match="different files"):
        write_outputs([(target, "text"), (target, "report")], [])


def test_failed_rollback_names_the_retained_backup(tmp_path, monkeypatch):
    first, second = tmp_path / "out.srt", tmp_path / "out.json"
    first.write_text("original srt")
    second.write_text("original report")
    real_replace = os.replace

    def fail_second_and_rollback(source, destination, *args, **kwargs):
        if destination == second or str(source).endswith(".bak"):
            raise OSError("injected publication / rollback failure")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_and_rollback)
    with pytest.raises(CutSyncError, match="rollback incomplete") as error:
        write_outputs([(first, "new srt"), (second, "new report")], [], overwrite=True)
    backups = list(tmp_path.glob(".*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "original srt"
    assert str(backups[0]) in str(error.value)
    assert second.read_text() == "original report"


def test_stream_serialization_failure_publishes_nothing(tmp_path):
    first, second = tmp_path / "out.srt", tmp_path / "out.json"

    def broken_chunks():
        yield "partial report"
        raise ValueError("injected encoder failure")

    with pytest.raises(ValueError, match="encoder"):
        write_outputs([(first, "valid srt"), (second, broken_chunks())], [])
    assert list(tmp_path.iterdir()) == []


def test_cleanup_error_does_not_hide_original_publication_failure(tmp_path, monkeypatch):
    first, second = tmp_path / "out.srt", tmp_path / "out.json"
    real_link = os.link
    real_unlink = type(first).unlink

    def fail_second(source, destination, *args, **kwargs):
        if destination == second:
            raise OSError("original publication problem")
        return real_link(source, destination, *args, **kwargs)

    def fail_temporary_cleanup(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("temporary cleanup problem")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_second)
    monkeypatch.setattr(type(first), "unlink", fail_temporary_cleanup)
    with pytest.raises(CutSyncError, match="original publication problem") as error:
        write_outputs([(first, "SRT"), (second, "JSON")], [])
    assert "temporary cleanup" in str(error.value)
    assert not first.exists() and not second.exists()


def test_cleanup_error_after_success_names_published_state(tmp_path, monkeypatch):
    first, second = tmp_path / "out.srt", tmp_path / "out.json"
    real_unlink = type(first).unlink

    def fail_temporary_cleanup(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("temporary cleanup problem")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(first), "unlink", fail_temporary_cleanup)
    with pytest.raises(CutSyncError, match="outputs were published"):
        write_outputs([(first, "SRT"), (second, "JSON")], [])
    assert first.read_text() == "SRT" and second.read_text() == "JSON"
