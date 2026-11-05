"""本机目录与祖先路径校验；扫描时仍需逐个检查目录项。"""

import os
from pathlib import Path
import stat


# 逐层检查原始目录及祖先不含符号链接，再返回规范化目录路径。
# 先检查未经解析的原始路径，再规范化，防止符号链接在解析后被掩盖。
def checked_directory(value: str | Path, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if must_exist:
                raise ValueError('Directory must already exist') from None
            # 当前层不存在时仍检查后续已有路径，避免后面的 .. 回退绕过链接校验。
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('Directory or ancestor is a symbolic link')
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('Expected a directory')
    normalized = Path(os.path.abspath(path))
    if normalized != path:
        return checked_directory(normalized, must_exist=must_exist)
    if must_exist and not normalized.is_dir():
        raise ValueError('Directory must already exist')
    return normalized


# 检查资料根与应用数据目录，确保数据库等应用数据位于资料根外。
def validate_data_root(data_dir: Path, root: Path) -> tuple[Path, Path]:
    data = checked_directory(data_dir, must_exist=False)
    authorized = checked_directory(root)
    if data.is_relative_to(authorized):
        raise ValueError('DATA_DIR must be outside ROOT_PATH')
    return data, authorized
