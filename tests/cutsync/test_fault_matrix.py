"""External filesystem invariants at each publication failure checkpoint."""

import errno
import os
import tempfile

import pytest

from mediawrench.cutsync.storage import write_outputs


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure_index", [1, 2])
@pytest.mark.parametrize("checkpoint", ["create", "flush", "backup", "publish", "reserve-unlink"])
def test_io_failure_preserves_original_pair_and_removes_staging(
    tmp_path,
    monkeypatch,
    force,
    failure_index,
    checkpoint,
):
    if not force and checkpoint in ("backup", "reserve-unlink"):
        pytest.skip("no backups in no-overwrite mode")
    first, second = tmp_path / "output.srt", tmp_path / "audit.json"
    if force:
        first.write_text("original subtitle")
        second.write_text("original audit")
    count = 0

    def fail_once():
        nonlocal count
        count += 1
        if count == failure_index:
            raise OSError(errno.EIO, f"injected {checkpoint} failure")

    original_temp = tempfile.NamedTemporaryFile
    original_sync, original_link, original_replace = os.fsync, os.link, os.replace
    original_unlink = type(first).unlink

    def create(*args, **kwargs):
        if checkpoint == "create":
            fail_once()
        return original_temp(*args, **kwargs)

    def flush(*args, **kwargs):
        if checkpoint == "flush":
            fail_once()
        return original_sync(*args, **kwargs)

    def link(source, destination, *args, **kwargs):
        if checkpoint == "backup" and str(destination).endswith(".bak"):
            fail_once()
        if checkpoint == "publish" and str(source).endswith(".tmp"):
            fail_once()
        return original_link(source, destination, *args, **kwargs)

    def replace(source, destination, *args, **kwargs):
        if checkpoint == "publish" and str(source).endswith(".tmp"):
            fail_once()
        return original_replace(source, destination, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if checkpoint == "reserve-unlink" and str(path).endswith(".bak"):
            fail_once()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", create)
    monkeypatch.setattr(os, "fsync", flush)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(type(first), "unlink", unlink)
    with pytest.raises(OSError, match="injected"):
        write_outputs([(first, "new subtitle"), (second, "new audit")], [], overwrite=force)
    if force:
        assert first.read_text() == "original subtitle"
        assert second.read_text() == "original audit"
        assert set(tmp_path.iterdir()) == {first, second}
    else:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("force", [False, True])
def test_keyboard_interrupt_during_second_publish_restores_state(tmp_path, monkeypatch, force):
    first, second = tmp_path / "output.srt", tmp_path / "audit.json"
    if force:
        first.write_text("old subtitle")
        second.write_text("old audit")
    original_link, original_replace = os.link, os.replace

    def link(source, destination, *args, **kwargs):
        if destination == second and str(source).endswith(".tmp"):
            raise KeyboardInterrupt
        return original_link(source, destination, *args, **kwargs)

    def replace(source, destination, *args, **kwargs):
        if destination == second and str(source).endswith(".tmp"):
            raise KeyboardInterrupt
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(KeyboardInterrupt):
        write_outputs([(first, "new subtitle"), (second, "new audit")], [], overwrite=force)
    assert not list(tmp_path.glob(".*.cut-sync-*"))
    if force:
        assert first.read_text() == "old subtitle" and second.read_text() == "old audit"
    else:
        assert list(tmp_path.iterdir()) == []
