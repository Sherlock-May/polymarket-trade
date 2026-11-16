"""同一数据库只能由一个运行实例管理，锁文件保留不会影响下次启动。"""
from pathlib import Path
import os


class ProcessLock:
    def __init__(self, path):
        self.path = Path(str(Path(path).resolve()) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("已有进程正在管理这个数据库") from None
        return self

    def __exit__(self, *args):
        self.file.close()
