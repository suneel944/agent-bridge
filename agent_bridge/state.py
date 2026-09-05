"""Small shared persistence helpers; safe to import in per-tool hooks."""

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


class BridgeError(Exception):
    """An actionable operational failure."""


def write_json(path: Path, value: dict) -> None:
    """Writes private JSON using atomic replacement.

    Args:
        path: Destination in an existing private directory.
        value: JSON-serializable state to publish.

    Raises:
        OSError: If writing or replacing the destination fails.
    """
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def lock(path: Path) -> Iterator[None]:
    """Holds an exclusive nonblocking lock for the context lifetime.

    Args:
        path: Lock file in an existing private directory.

    Yields:
        None while the caller holds the operation lock.

    Raises:
        BridgeError: If another process holds the lock.
    """
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError(
                f"Another bridge operation/session owns {path.name}; "
                "retry later."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
