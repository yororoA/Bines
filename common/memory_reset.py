"""离线重置实例数据；中断时保留标记，避免用新旧混合的数据启动。"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid


PERSONA_ONLY_MARKER = ".persona-only"


def pending_path(directory: Path) -> Path:
    return directory.with_name("memory_data.reset-pending")


@contextmanager
def reset_directory(directory: Path):
    pending = pending_path(directory)
    for path in (directory, pending):
        if path.resolve() != path or path.is_symlink() or path.is_mount():
            raise ValueError(f"不能重置链接或挂载点：{path}")
        if path.exists() and not path.is_dir():
            raise ValueError(f"实例数据路径不是目录：{path}")

    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.exists():
        directory.rename(pending / uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    (directory / PERSONA_ONLY_MARKER).touch(mode=0o600)
    yield
    shutil.rmtree(pending)
