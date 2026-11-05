"""同一应用数据目录在服务或命令运行期间的进程独占锁。"""

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


# 在整个使用期间独占同一数据目录的操作系统锁，并在退出时释放。
@contextmanager
def data_directory_lock(data_dir: Path):
    directory = data_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # 不删除重建锁文件，以免两个进程锁到不同 inode；进程退出后操作系统释放实际锁。
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
