"""Lifetime lock for the single executor associated with a data directory."""

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


@contextmanager
def data_directory_lock(data_dir: Path):
    directory = data_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place: deleting/recreating it can let contenders
    # lock different inodes. The OS releases the actual lock on process exit.
    lock = FileLock(directory / '.executor.lock', timeout=0,
                    preserve_lock_file=True, fallback_to_soft=False)
    try:
        lock.acquire()
    except Timeout:
        raise RuntimeError('此 DATA_DIR 已有服务运行，请先停止原服务。') from None
    try:
        yield
    finally:
        lock.release()
