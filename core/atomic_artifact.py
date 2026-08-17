"""Crash-safe publication for immutable workflow artifacts."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable


PENDING_ARTIFACT_PREFIX = ".pending-artifact-"


def is_pending_artifact(path: Path) -> bool:
    """Return whether a path is an uncommitted atomic-write temporary file."""
    return Path(path).name.startswith(PENDING_ARTIFACT_PREFIX)


def publish_immutable_bytes(
        path: Path, content: bytes,
        conflict: Callable[[str], Exception],
        message: str) -> None:
    """Publish complete bytes atomically without replacing prior evidence.

    The temporary inode is fully flushed before a hard link makes it visible
    at the authoritative path.  A crash can therefore leave an ignored hidden
    temporary file, but never a partially written authoritative artifact.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if (not target.is_file() or target.is_symlink() or
                target.read_bytes() != content):
            raise conflict(message)
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=PENDING_ARTIFACT_PREFIX, dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if (not target.is_file() or target.is_symlink() or
                    target.read_bytes() != content):
                raise conflict(message)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def publish_immutable_text(
        path: Path, content: str,
        conflict: Callable[[str], Exception],
        message: str) -> None:
    publish_immutable_bytes(
        path, content.encode("utf-8"), conflict, message)


def publish_replaceable_bytes(path: Path, content: bytes) -> None:
    """Atomically replace a disposable derived artifact after full fsync."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=PENDING_ARTIFACT_PREFIX, dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "PENDING_ARTIFACT_PREFIX", "is_pending_artifact",
    "publish_immutable_bytes", "publish_immutable_text",
    "publish_replaceable_bytes",
]
