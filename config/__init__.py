"""配置与用户加载"""
import base64
import hashlib
import hmac
import json
import secrets
import re
from contextlib import contextmanager
from pathlib import Path
from threading import local

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_USERNAME = "hekaiyu"
DEFAULT_CONFIG_PATH = CONFIG_DIR / f"{DEFAULT_USERNAME}.yaml"
USERS_PATH = CONFIG_DIR / "users.json"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

_config_cache = {}
_active_user = local()


def _get_request_username():
    try:
        from flask import has_request_context, session
    except Exception:
        return None
    if not has_request_context():
        return None
    username = session.get("username")
    return username.strip() if isinstance(username, str) and username.strip() else None


def get_current_username(default=DEFAULT_USERNAME):
    request_username = _get_request_username()
    if request_username:
        return request_username
    thread_username = getattr(_active_user, "username", None)
    if thread_username:
        return thread_username
    return default


def set_active_username(username: str):
    _active_user.username = (username or "").strip() or DEFAULT_USERNAME


def clear_active_username():
    if hasattr(_active_user, "username"):
        delattr(_active_user, "username")


@contextmanager
def use_active_username(username: str):
    previous = getattr(_active_user, "username", None)
    set_active_username(username)
    try:
        yield
    finally:
        if previous:
            _active_user.username = previous
        else:
            clear_active_username()


def get_config_path(username=None) -> Path:
    username = (username or get_current_username()).strip() or DEFAULT_USERNAME
    return CONFIG_DIR / f"{username}.yaml"


def list_config_usernames():
    return sorted(
        path.stem
        for path in CONFIG_DIR.glob("*.yaml")
        if path.is_file()
    )


def load_config(username=None):
    """加载 YAML 配置，按用户名区分。"""
    config_path = get_config_path(username)
    cache_key = str(config_path)
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        mtime = None

    cached = _config_cache.get(cache_key)
    if cached and cached["mtime"] == mtime:
        return cached["data"]

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    _config_cache[cache_key] = {"mtime": mtime, "data": data}
    return data


def invalidate_config_cache(username=None):
    if username:
        _config_cache.pop(str(get_config_path(username)), None)
        return
    _config_cache.clear()


def set_config_value(config_data: dict, key: str, value):
    cursor = config_data
    parts = key.split(".")
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def update_user_config(username: str, updates: dict):
    username = (username or get_current_username()).strip() or DEFAULT_USERNAME
    config_path = ensure_user_config(username)
    config_data = load_config(username=username)
    if not isinstance(config_data, dict):
        config_data = {}

    for key, value in (updates or {}).items():
        if not key:
            continue
        set_config_value(config_data, key, value)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)
    invalidate_config_cache(username)
    return config_path


def get(key: str, default=None, username=None):
    """获取配置项，支持点号路径如 model.api_key"""
    cfg = load_config(username=username)
    for k in key.split("."):
        if not isinstance(cfg, dict):
            return default
        cfg = cfg.get(k)
    return default if cfg is None else cfg


def resolve_path(path: str) -> Path:
    """解析路径，相对路径则基于项目根目录"""
    p = Path(path)
    return (BASE_DIR / p) if not p.is_absolute() else p


def load_users_store():
    if not USERS_PATH.exists():
        return {"users": {}}
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {"users": {}}
    users = data.get("users", {})
    if not isinstance(users, dict):
        users = {}
    return {"users": users}


def save_users_store(store):
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": store.get("users", {})}
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=4)


def validate_username(username: str):
    cleaned = (username or "").strip()
    if not USERNAME_PATTERN.fullmatch(cleaned):
        raise ValueError("用户名仅支持 3-32 位字母、数字、下划线或连字符")
    return cleaned


def validate_password(password: str):
    if len(password or "") < 6:
        raise ValueError("密码至少需要 6 位")
    return password


def _hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        salt_b64, digest_b64 = encoded.split("$", 1)
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(digest_b64.encode())
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return hmac.compare_digest(actual, expected)


def build_default_user_config():
    template = load_config(DEFAULT_USERNAME)
    config_data = {
        "model": {
            "api_key": "",
            "model": ((template.get("model") or {}).get("model") if isinstance(template, dict) else "") or "deepseek-chat",
        },
        "file": {
            "save_path": get("file.save_path", str(BASE_DIR / "file"), username=DEFAULT_USERNAME),
            "area": get("file.area", "RO", username=DEFAULT_USERNAME),
        },
        "ui": {
            "port": get("ui.port", 5715, username=DEFAULT_USERNAME),
            "host": get("ui.host", "0.0.0.0", username=DEFAULT_USERNAME),
            "debug": get("ui.debug", True, username=DEFAULT_USERNAME),
        },
        "summary": {
            "user_question": "",
        },
    }
    return config_data


def ensure_user_config(username: str):
    username = validate_username(username)
    config_path = get_config_path(username)
    if config_path.exists():
        return config_path
    config_data = build_default_user_config()
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)
    invalidate_config_cache(username)
    return config_path


def register_user(username: str, password: str):
    username = validate_username(username)
    validate_password(password)
    store = load_users_store()
    users = store["users"]
    if username in users:
        raise ValueError("用户名已存在")
    config_path = ensure_user_config(username)
    users[username] = {
        "password_hash": _hash_password(password),
        "config_path": str(config_path),
    }
    save_users_store(store)
    return config_path


def authenticate_user(username: str, password: str) -> bool:
    username = validate_username(username)
    store = load_users_store()
    user = store["users"].get(username)
    if not user:
        return False
    return _verify_password(password, user.get("password_hash", ""))
