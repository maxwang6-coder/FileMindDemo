"""启动配置：根目录仅由本机 TOML 或 CLI 授权，不接收 HTTP、环境变量或 .env 的根授权。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Sequence

from app.paths import checked_directory, validate_data_root


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_DIR / "config.local.toml"


@dataclass(frozen=True, slots=True)
class Settings:
    DATA_DIR: Path = PROJECT_DIR / "data"
    ROOT_PATH: Path | None = None
    CLOUD_ENABLED: bool = False
    MODEL_PROVIDER: str = "mock"
    WRITE_ENABLED: bool = False
    MAX_FILES: int = 500

# 按指定基准目录解析配置路径，并检查目录及祖先是否合规。
def _directory(value: object, base: Path, name: str, *, required: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty directory path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return checked_directory(path, must_exist=required)


# 合并默认值、本机 TOML 与 CLI 授权，校验后生成启动配置。
# TOML 相对路径按配置文件目录解释，CLI 相对路径按工作目录解释；不从网页参数获得授权。
def load_settings(argv: Sequence[str] = ()) -> Settings:
    # 1. 解析配置文件与根授权参数；未显式指定配置时只尝试默认本机配置。
    parser = argparse.ArgumentParser(description="FileMindDemo configuration")
    parser.add_argument("--config", type=Path, help="Local TOML configuration")
    parser.add_argument("--root-path", help="Locally authorize a filesystem root")
    args = parser.parse_args(argv)
    config_file = (args.config or DEFAULT_CONFIG_FILE).expanduser().resolve()
    values = {}
    if args.config is not None or config_file.exists():
        with config_file.open("rb") as source:
            values = tomllib.load(source)

    allowed = set(Settings.__dataclass_fields__)
    # 2. 拒绝未知字段和错误类型，检查文件数上限与模型、云开关的组合。
    unknown = values.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    for name in ("CLOUD_ENABLED", "WRITE_ENABLED"):
        if name in values and type(values[name]) is not bool:
            raise ValueError(f"{name} must be a boolean")
    maximum = values.get("MAX_FILES", 500)
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("MAX_FILES must be a positive integer")
    provider = values.get("MODEL_PROVIDER", "mock")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("MODEL_PROVIDER must be a non-empty string")
    if not values.get("CLOUD_ENABLED", False) and provider != "mock":
        raise ValueError("Only MODEL_PROVIDER=mock is supported when cloud is disabled")
    # 3. 按配置文件所在目录解析 TOML 路径，再用 CLI 根授权覆盖 TOML 的根设置。
    if "DATA_DIR" in values:
        values["DATA_DIR"] = _directory(values["DATA_DIR"], config_file.parent, "DATA_DIR", required=False)
    if "ROOT_PATH" in values:
        values["ROOT_PATH"] = _directory(values["ROOT_PATH"], config_file.parent, "ROOT_PATH", required=True)
    if args.root_path is not None:
        values["ROOT_PATH"] = _directory(args.root_path, Path.cwd(), "ROOT_PATH", required=True)
    configuration = Settings(**values)
    # 4. 构造最终配置并验证应用数据位于资料根外，不在加载时创建数据库或扫描资料。
    if configuration.ROOT_PATH is not None:
        validate_data_root(configuration.DATA_DIR, configuration.ROOT_PATH)
    return configuration


# 保存不可变启动配置快照；网页处理器不能通过查询参数、请求体或上传文件重新加载根授权。
settings = load_settings()
DATA_DIR = settings.DATA_DIR
ROOT_PATH = settings.ROOT_PATH
CLOUD_ENABLED = settings.CLOUD_ENABLED
MODEL_PROVIDER = settings.MODEL_PROVIDER
WRITE_ENABLED = settings.WRITE_ENABLED
MAX_FILES = settings.MAX_FILES
