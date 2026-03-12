"""配置加载"""
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "hekaiyu.yaml"

_config = None


def load_config():
    """加载 YAML 配置"""
    global _config
    if _config is None:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _config = yaml.safe_load(f) or {}
        else:
            _config = {}
    return _config


def get(key: str, default=None):
    """获取配置项，支持点号路径如 model.api_key"""
    cfg = load_config()
    for k in key.split("."):
        if not isinstance(cfg, dict):
            return default
        cfg = cfg.get(k)
    return default if cfg is None else cfg


def resolve_path(path: str) -> Path:
    """解析路径，相对路径则基于项目根目录"""
    p = Path(path)
    return (BASE_DIR / p) if not p.is_absolute() else p
