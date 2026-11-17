"""Stage both artifacts before publishing; roll back ordinary write failures."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .core import CutSyncError


def _aliases(first: Path, second: Path) -> bool:
    try:
        same_path = first.resolve() == second.resolve()
    except (RuntimeError, OSError) as exc:
        raise CutSyncError(f"cannot resolve input/output path: {exc}") from exc
    if same_path:
        return True
    return first.exists() and second.exists() and first.samefile(second)


def validate_destinations(
    destinations: list[Path],
    protected: list[Path],
    *,
    overwrite: bool,
) -> None:
    for i, path in enumerate(destinations):
        if any(_aliases(path, source) for source in protected):
            raise CutSyncError(f"output must not replace an input: {path}")
        if any(_aliases(path, other) for other in destinations[:i]):
            raise CutSyncError("SRT output and report must be different files")
        if not path.parent.is_dir():
            raise CutSyncError(f"output directory does not exist: {path.parent}")
        if path.is_symlink():
            raise CutSyncError(f"refusing a symlink output: {path}")
        if path.exists() and not path.is_file():
            raise CutSyncError(f"output is not a regular file: {path}")
        if path.exists() and not overwrite:
            raise CutSyncError(f"output already exists: {path}; choose another path or use --force")


def write_outputs(
    payloads: list[tuple[Path, str | Iterable[str]]],
    protected: list[Path],
    *,
    overwrite: bool = False,
) -> None:
    """Best-effort two-file transaction, not atomic across a crash/power loss.

    In no-overwrite mode a hard link publishes each complete staged file
    exclusively, so a concurrent file creator is never clobbered. In force
    mode existing regular files are backed up until both writes succeed.
    """
    destinations = [path for path, _ in payloads]
    validate_destinations(destinations, protected, overwrite=overwrite)
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    reserved_backups: list[Path] = []
    published: list[Path] = []
    succeeded = False
    active_error: BaseException | None = None
    try:
        for path, text in payloads:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.cut-sync-",
                suffix=".tmp",
                delete=False,
            ) as fp:
                staged[path] = Path(fp.name)
                if isinstance(text, str):
                    fp.write(text)
                else:
                    fp.writelines(text)
                fp.flush()
                os.fsync(fp.fileno())
        # Recheck after staging, including aliases and symlinks.
        validate_destinations(destinations, protected, overwrite=overwrite)
        if overwrite:
            for path in destinations:
                if path.exists():
                    fd, name = tempfile.mkstemp(
                        dir=path.parent,
                        prefix=f".{path.name}.cut-sync-",
                        suffix=".bak",
                    )
                    backup = Path(name)
                    reserved_backups.append(backup)
                    os.close(fd)
                    backup.unlink()
                    os.link(path, backup)
                    backups[path] = backup
        for path in destinations:
            if overwrite:
                os.replace(staged[path], path)
            else:
                os.link(staged[path], path)
            published.append(path)
        succeeded = True
    except BaseException as original:
        active_error = original
        recovery_errors = []
        for path in reversed(published):
            try:
                if path in backups:
                    os.replace(backups[path], path)
                    del backups[path]
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                recovery_errors.append(f"{path}: {exc}")
        if recovery_errors:
            retained = [backup for path, backup in backups.items() if path in published]
            active_error = CutSyncError(
                f"write failed ({original}); rollback incomplete: {'; '.join(recovery_errors)}; "
                f"recover from backups: {retained}"
            )
            raise active_error from original
        raise
    finally:
        cleanup_errors = []

        def cleanup(path: Path) -> None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(f"{path}: {exc}")

        for temporary in staged.values():
            cleanup(temporary)
        # Track even empty reserved backup names before closing/unlinking them.
        # Only a failed-restore backup of a published destination is retained.
        retained_backups = (
            set()
            if succeeded
            else {backup for path, backup in backups.items() if path in published}
        )
        for backup in reserved_backups:
            if backup not in retained_backups:
                cleanup(backup)
        if cleanup_errors:
            state = str(active_error) if active_error is not None else "outputs were published"
            raise CutSyncError(
                f"{state}; temporary cleanup failed: {'; '.join(cleanup_errors)}"
            ) from active_error
