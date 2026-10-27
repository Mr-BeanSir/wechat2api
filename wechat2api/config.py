"""配置加载。

优先级: 环境变量 > config.yaml > 代码默认值。
环境变量名规则: ``WECHAT2API__<段名>__<字段名>``（全大写），例如
``WECHAT2API__WECHAT__TARGET=文件传输助手``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_DIR / "config.yaml"
ENV_PREFIX = "WECHAT2API__"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8800
    api_key: str = ""
    model_name: str = "wechat-2api"


@dataclass
class RedisConfig:
    url: str = "redis://127.0.0.1:6379/0"
    prefix: str = "wechat2api"
    require: bool = False
    ttl_seconds: int = 0


@dataclass
class WeChatConfig:
    target: str = "微信支付"
    init_command: str = "/clear"
    prompt_file: str = "prompt.txt"
    exact: bool = False
    send_timeout: float = 60.0
    reply_timeout: float = 120.0
    reply_settle: float = 3.0
    init_command_timeout: float = 60.0
    init_prompt_timeout: float = 120.0
    poll_interval: float = 0.5
    reinit_on_switch: bool = True
    init_require_reply: bool = True
    # 拟人节流档位（wechatauto.rhythm）: off / fast / natural / calm。
    # 默认 off —— 每次点击/键入之间不加人为等待。空串 "" = 不干预库的默认档。
    rhythm: str = "off"


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)
    config_file: Optional[str] = None

    # -- 派生值 ---------------------------------------------------------------
    @property
    def prompt_path(self) -> Path:
        p = Path(self.wechat.prompt_file)
        return p if p.is_absolute() else PROJECT_DIR / p

    def prompt_text(self) -> str:
        """读取 prompt.txt；文件不存在时抛 FileNotFoundError。"""
        return self.prompt_path.read_text(encoding="utf-8").strip()


def _coerce(target_type: Any, value: Any) -> Any:
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "y")
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _apply(section: Any, data: Dict[str, Any], where: str) -> None:
    # 注意: 本模块用了 `from __future__ import annotations`，field.type 是字符串，
    # 必须用 get_type_hints 还原成真正的类型，否则 bool/int 强转全部失效。
    import typing

    hints = typing.get_type_hints(type(section))
    valid = {f.name for f in fields(section)}
    for key, value in (data or {}).items():
        if key not in valid:
            continue  # 未知字段忽略，避免旧配置把程序弄崩
        try:
            setattr(section, key, _coerce(hints.get(key, str), value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {where}.{key} 取值非法: {value!r} ({exc})") from exc


def _apply_env(cfg: Config) -> None:
    for name, value in os.environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        rest = name[len(ENV_PREFIX):]
        if "__" not in rest:
            continue
        section_name, field_name = rest.split("__", 1)
        section = getattr(cfg, section_name.strip().lower(), None)
        if section is None or not is_dataclass(section):
            continue
        _apply(section, {field_name.strip().lower(): value}, f"env.{section_name}")


def load_config(path: Optional[str] = None) -> Config:
    cfg = Config()
    target = Path(path) if path else DEFAULT_CONFIG_FILE
    if target.exists():
        import yaml  # 延迟导入，缺 yaml 时只有用到配置文件才报错

        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"配置文件格式不对: {target}")
        cfg.config_file = str(target)
        for name in ("server", "redis", "wechat"):
            section = getattr(cfg, name)
            _apply(section, raw.get(name) or {}, name)
    _apply_env(cfg)
    return cfg
