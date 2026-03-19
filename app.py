"""
论文预览网页应用
支持通过 IP 地址访问，展示论文标题、摘要和链接
"""
import html
import json
import os
import re
from calendar import monthrange
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from time import sleep, time
from typing import Optional
from urllib.parse import urlencode, urlparse

from flask import Flask, redirect, render_template, request, session
try:
    import markdown as python_markdown
except ImportError:
    python_markdown = None

from config import (
    authenticate_user,
    get,
    get_config_path,
    get_current_username,
    load_config,
    register_user,
    resolve_path,
    update_user_config,
    use_active_username,
)
from model.api import load_model
from process_file.analysis_paper import analysis_paper, get_analysis_output_path
from process_file.load_papers import download_papers_today
from process_file.summary_all_papers import summary_all_papers
from process_file.translate_title_abstract import get_pending_translation_count, translate_papers

app = Flask(__name__, template_folder="ui")
app.secret_key = get("ui.secret_key", "read-paper-dev-secret", username="hekaiyu")
AUTO_UPDATE_TASK_LOCK = Lock()
AUTO_UPDATE_RUNNING = False
AUTO_UPDATE_CHECK_INTERVAL_SECONDS = 3600
BALANCE_CACHE_LOCK = Lock()
BALANCE_CACHE = {"value": None, "updated_at": 0.0}
PAPERS_CACHE_LOCK = Lock()
PAPERS_CACHE = {}
PAPERS_METADATA_CACHE_LOCK = Lock()
PAPERS_METADATA_CACHE = {}
SUMMARY_CACHE_LOCK = Lock()
SUMMARY_CACHE = {}
ANALYSIS_CACHE_LOCK = Lock()
ANALYSIS_CACHE = {}
AVAILABLE_DATES_CACHE_LOCK = Lock()
AVAILABLE_DATES_CACHE = {"value": None, "updated_at": 0.0}
COLLECTIONS_CACHE_LOCK = Lock()
COLLECTIONS_CACHE = {}
SUMMARY_JOB_LOCK = Lock()
SUMMARY_JOB_STATUS = {}
ANALYSIS_JOB_LOCK = Lock()
ANALYSIS_JOB_STATUS = {}
AUTO_UPDATE_SCHEDULER_LOCK = Lock()
AUTO_UPDATE_SCHEDULER_STARTED = False
SETTINGS_TASK_LOCK = Lock()
SETTINGS_TASK_STATUS = {
    "update_papers": False,
    "translate_papers": False,
}


def get_file_dir():
    save_path = get("file.save_path", "file")
    return resolve_path(save_path)


def get_api_balance():
    """查询 API 余额，并做短时缓存以避免每次刷新都请求接口。"""
    cache_ttl = int(get("model.balance_cache_seconds", 300) or 300)
    now_ts = time()
    if BALANCE_CACHE["updated_at"] and now_ts - BALANCE_CACHE["updated_at"] < cache_ttl:
        return BALANCE_CACHE["value"]

    with BALANCE_CACHE_LOCK:
        now_ts = time()
        if BALANCE_CACHE["updated_at"] and now_ts - BALANCE_CACHE["updated_at"] < cache_ttl:
            return BALANCE_CACHE["value"]

        balance_value = None
        try:
            model_api = load_model()
            if model_api:
                balance_value = model_api.get_balance()
        except Exception as exc:
            print(f"余额查询失败: {exc}")

        BALANCE_CACHE["value"] = balance_value
        BALANCE_CACHE["updated_at"] = now_ts
        return balance_value


def get_balance_cache_value():
    return BALANCE_CACHE["value"]


def get_today_dir(now=None):
    now = now or datetime.now()
    return get_file_dir() / str(now.year) / str(now.month) / str(now.day)


def get_area_names():
    raw_area = get("file.area", "RO")
    if isinstance(raw_area, list):
        areas = [str(item).strip() for item in raw_area if str(item).strip()]
    else:
        areas = [item.strip() for item in str(raw_area or "RO").split(",") if item.strip()]
    return areas or ["RO"]


def get_area_name() -> str:
    return get_area_names()[0]


def get_area_dir(day_dir: Path, area_name: Optional[str] = None) -> Path:
    return day_dir / (area_name or get_area_name())


def read_json_file(json_path: Path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return None


def get_file_mtime(json_path: Path):
    try:
        return json_path.stat().st_mtime
    except OSError:
        return None


def load_cached_json(json_path: Path):
    mtime = get_file_mtime(json_path)
    if mtime is None:
        return None

    cache_key = str(json_path)
    with PAPERS_CACHE_LOCK:
        cached = PAPERS_CACHE.get(cache_key)
        if cached and cached["mtime"] == mtime:
            return cached["data"]

    data = read_json_file(json_path)
    if data is None:
        return None

    with PAPERS_CACHE_LOCK:
        PAPERS_CACHE[cache_key] = {"mtime": mtime, "data": data}
    return data


def has_papers_content(json_path: Optional[Path]) -> bool:
    if not json_path or not json_path.exists():
        return False
    data = load_cached_json(json_path)
    return bool((data or {}).get("papers"))


def get_cached_papers_metadata(json_path: Path):
    data = load_cached_json(json_path)
    if data is None:
        return None

    mtime = get_file_mtime(json_path)
    if mtime is None:
        return None

    cache_key = str(json_path)
    with PAPERS_METADATA_CACHE_LOCK:
        cached = PAPERS_METADATA_CACHE.get(cache_key)
        if cached and cached["mtime"] == mtime:
            return cached["data"]

    papers = data.get("papers", [])
    prepared_papers = []
    paper_index = {}
    all_tags = set()

    for paper in papers:
        topics = [
            tag.strip()
            for tag in paper.get("topics_zh", [])
            if isinstance(tag, str) and tag.strip()
        ]
        prepared_paper = {
            **paper,
            "_topic_set": set(topics),
        }
        prepared_papers.append(prepared_paper)
        paper_index[get_paper_id(prepared_paper)] = prepared_paper
        all_tags.update(topics)

    metadata = {
        "papers": prepared_papers,
        "paper_index": paper_index,
        "all_tags": sorted(all_tags),
    }

    with PAPERS_METADATA_CACHE_LOCK:
        PAPERS_METADATA_CACHE[cache_key] = {"mtime": mtime, "data": metadata}
    return metadata


def invalidate_available_dates_cache():
    with AVAILABLE_DATES_CACHE_LOCK:
        AVAILABLE_DATES_CACHE["value"] = None
        AVAILABLE_DATES_CACHE["updated_at"] = 0.0


def get_collect_dir():
    return get_file_dir() / "collect"


def get_collections_path():
    return get_collect_dir() / "collections.json"


def get_current_request_url() -> str:
    query_string = request.query_string.decode("utf-8")
    return f"{request.path}?{query_string}" if query_string else request.path


def pop_auth_feedback():
    return {
        "auth_error": session.pop("auth_error", ""),
        "auth_notice": session.pop("auth_notice", ""),
        "auth_mode": session.pop("auth_mode", "login"),
        "auth_popup_open": bool(session.pop("auth_popup_open", False)),
    }


def store_auth_feedback(error="", notice="", mode="login", open_popup=False):
    if error:
        session["auth_error"] = error
    else:
        session.pop("auth_error", None)

    if notice:
        session["auth_notice"] = notice
    else:
        session.pop("auth_notice", None)

    session["auth_mode"] = mode if mode in {"login", "register"} else "login"
    session["auth_popup_open"] = bool(open_popup)


def clear_auth_feedback():
    session.pop("auth_error", None)
    session.pop("auth_notice", None)
    session.pop("auth_mode", None)
    session.pop("auth_popup_open", None)


def resolve_redirect_to(redirect_to: str, lang: str, fallback_path="/settings"):
    fallback = build_query_string(base_path=fallback_path, lang=lang)
    redirect_to = (redirect_to or "").strip()
    if not redirect_to:
        return fallback
    parsed = urlparse(redirect_to)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not redirect_to.startswith("/"):
        return fallback
    return redirect_to


def infer_source_date(source_path: Optional[Path]) -> str:
    if not source_path:
        return ""
    try:
        day_dir = source_path.parent
        if day_dir.name in set(get_area_names()):
            day_dir = day_dir.parent
        return date_label_from_parts(day_dir.parent.parent.name, day_dir.parent.name, day_dir.name)
    except (ValueError, AttributeError, IndexError):
        return ""


def load_collections_store():
    collections_path = get_collections_path()
    if not collections_path.exists():
        return {"papers": []}

    mtime = get_file_mtime(collections_path)
    if mtime is None:
        return {"papers": []}

    cache_key = str(collections_path)
    with COLLECTIONS_CACHE_LOCK:
        cached = COLLECTIONS_CACHE.get(cache_key)
        if cached and cached["mtime"] == mtime:
            return cached["data"]

    data = read_json_file(collections_path) or {}
    papers = data.get("papers", [])
    if not isinstance(papers, list):
        papers = []

    normalized = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        paper_url = str(paper.get("url", "")).strip()
        if not paper_url:
            continue
        normalized.append({**paper, "url": paper_url})

    normalized.sort(key=lambda item: item.get("collected_at", ""), reverse=True)
    store = {"papers": normalized}
    with COLLECTIONS_CACHE_LOCK:
        COLLECTIONS_CACHE[cache_key] = {"mtime": mtime, "data": store}
    return store


def save_collections_store(papers):
    collections_path = get_collections_path()
    collections_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = sorted(papers, key=lambda item: item.get("collected_at", ""), reverse=True)
    payload = {"papers": normalized}
    with open(collections_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=4)

    mtime = get_file_mtime(collections_path)
    if mtime is not None:
        with COLLECTIONS_CACHE_LOCK:
            COLLECTIONS_CACHE[str(collections_path)] = {"mtime": mtime, "data": payload}


def get_collected_url_set():
    return {
        paper.get("url", "").strip()
        for paper in load_collections_store()["papers"]
        if isinstance(paper, dict) and paper.get("url")
    }


def build_collection_entry(paper: dict, source_path: Optional[Path]):
    return {
        "title": paper.get("title", ""),
        "title_zh": paper.get("title_zh", ""),
        "abstract": paper.get("abstract", ""),
        "abstract_zh": paper.get("abstract_zh", ""),
        "url": paper.get("url", ""),
        "topics_zh": [
            tag.strip()
            for tag in paper.get("topics_zh", [])
            if isinstance(tag, str) and tag.strip()
        ],
        "source_path": str(source_path) if source_path else "",
        "source_date": infer_source_date(source_path),
        "collected_at": datetime.now().isoformat(),
    }


def find_paper_in_source(source_path: Optional[Path], paper_url: str):
    if not source_path or not source_path.exists() or not paper_url:
        return None
    metadata = get_cached_papers_metadata(source_path)
    if metadata is None:
        return None
    return metadata["paper_index"].get(paper_url)


def build_base_page_data(lang: str, search_query: str, view: str):
    current_username = get_current_username()
    auth_feedback = pop_auth_feedback()
    return {
        "lang": lang,
        "api_balance": get_balance_cache_value(),
        "summary_response": None,
        "summary_status": build_summary_status(None, get_summary_question(), False),
        "summary_question": get_summary_question(),
        "search_query": (search_query or "").strip(),
        "current_url": get_current_request_url(),
        "view": view,
        "home_href": build_query_string(lang=lang),
        "favorites_href": build_query_string(base_path="/favorites", lang=lang),
        "settings_href": build_query_string(base_path="/settings", lang=lang),
        "login_href": build_query_string(base_path="/login", lang=lang),
        "register_href": build_query_string(base_path="/register", lang=lang),
        "logout_href": build_query_string(base_path="/logout", lang=lang),
        "is_authenticated": bool(session.get("username")),
        "current_username": current_username,
        "config_path": str(get_config_path(current_username)),
        **auth_feedback,
    }


def build_settings_data(lang="zh"):
    page_data = build_base_page_data(lang, "", "settings")
    current_config = load_config()
    model_cfg = current_config.get("model", {}) if isinstance(current_config, dict) else {}
    file_cfg = current_config.get("file", {}) if isinstance(current_config, dict) else {}
    ui_cfg = current_config.get("ui", {}) if isinstance(current_config, dict) else {}
    summary_cfg = current_config.get("summary", {}) if isinstance(current_config, dict) else {}
    model_options = ["deepseek-chat", "deepseek-reasoner"]
    current_model = str(model_cfg.get("model", "") or "deepseek-chat")
    if current_model not in model_options:
        current_model = "deepseek-chat"
    settings_items = [
        {
            "label": "当前用户",
            "value": page_data["current_username"],
        },
        {
            "label": "模型",
            "value": current_model or "未设置",
        },
        {
            "label": "总结关注问题",
            "value": get_summary_question() or "未设置",
        },
        {
            "label": "调试模式",
            "value": "开启" if get("ui.debug", True) else "关闭",
        },
        {
            "label": "自动更新时间",
            "value": get_update_schedule_time()[2],
        },
    ]
    settings_form = {
        "model_api_key": "",
        "model_model": current_model,
        "summary_user_question": str(summary_cfg.get("user_question", "") or ""),
        "file_area": str(file_cfg.get("area", "") or ""),
        "file_update_time": str(file_cfg.get("update_time", "10:00") or "10:00"),
        "ui_debug": bool(ui_cfg.get("debug", True)),
    }
    return {
        **page_data,
        "settings_items": settings_items,
        "settings_form": settings_form,
        "model_options": model_options,
        "settings_error": "",
        "settings_notice": "",
    }


def resolve_non_empty_papers_path(day_dir: Path):
    candidates = []
    for area_name in get_area_names():
        area_dir = get_area_dir(day_dir, area_name)
        candidates.extend([
            area_dir / "papers_zh.json",
            area_dir / "papers.json",
        ])
    candidates.extend([
        day_dir / "papers_zh.json",
        day_dir / "papers.json",
    ])
    for candidate in candidates:
        if has_papers_content(candidate):
            return candidate
    return None


def get_update_schedule_time():
    """读取每日自动更新的时间，格式为 HH:MM，默认 10:00。"""
    raw = str(get("file.update_time", "10:00") or "").strip()
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", raw)
    if not match:
        return 10, 0, "10:00"
    hour = int(match.group(1))
    minute = int(match.group(2))
    return hour, minute, f"{hour:02d}:{minute:02d}"


def run_today_papers_ready(now: datetime, today_label: str, username: str):
    global AUTO_UPDATE_RUNNING

    with use_active_username(username):
        today_dir = get_today_dir(now)
        area_names = get_area_names()
        area_paths = [
            (
                area_name,
                get_area_dir(today_dir, area_name) / "papers.json",
                get_area_dir(today_dir, area_name) / "papers_zh.json",
            )
            for area_name in area_names
        ]

        try:
            need_fetch = any(not has_papers_content(papers_path) for _, papers_path, _ in area_paths)
            if need_fetch:
                print(f"{today_label} 自动任务：检测到今日论文未完整拉取，开始自动拉取")
                download_papers_today()
                invalidate_available_dates_cache()
            else:
                print(f"{today_label} 自动任务：今日论文已拉取，跳过拉取")

            translated_any = False
            for area_name, papers_path, translation_path in area_paths:
                if not has_papers_content(papers_path):
                    continue
                pending_count = get_pending_translation_count(papers_path, translation_path)
                if pending_count > 0:
                    translated_any = True
                    print(f"{today_label} [{area_name}] 待翻译 {pending_count} 篇，开始自动翻译")
                    translate_papers(str(papers_path))
            if translated_any:
                print(f"{today_label} 自动任务：已完成待翻译论文补齐")
            else:
                print(f"{today_label} 自动任务：无待翻译论文，跳过翻译")
        except Exception as exc:
            print(f"今日论文自动补跑失败: {exc}")
        finally:
            with AUTO_UPDATE_TASK_LOCK:
                AUTO_UPDATE_RUNNING = False


def is_settings_task_running(task_name: str) -> bool:
    with SETTINGS_TASK_LOCK:
        return bool(SETTINGS_TASK_STATUS.get(task_name))


def set_settings_task_running(task_name: str, running: bool):
    with SETTINGS_TASK_LOCK:
        SETTINGS_TASK_STATUS[task_name] = running


def run_manual_update_papers(username: str):
    set_settings_task_running("update_papers", True)
    try:
        with use_active_username(username):
            print("设置页触发：开始更新今日论文列表")
            download_papers_today()
            invalidate_available_dates_cache()
            print("设置页触发：今日论文列表更新完成")
    except Exception as exc:
        print(f"设置页触发：更新论文列表失败: {exc}")
    finally:
        set_settings_task_running("update_papers", False)


def run_manual_translate_papers(username: str):
    set_settings_task_running("translate_papers", True)
    try:
        with use_active_username(username):
            now = datetime.now()
            today_dir = get_today_dir(now)
            translated_any = False
            for area_name in get_area_names():
                papers_path = get_area_dir(today_dir, area_name) / "papers.json"
                if not papers_path.exists():
                    continue
                translated_any = True
                print(f"设置页触发：开始翻译 {area_name} 今日论文")
                translate_papers(str(papers_path))
            if translated_any:
                print("设置页触发：今日论文翻译完成")
            else:
                print("设置页触发：未找到今日论文列表，跳过翻译")
    except Exception as exc:
        print(f"设置页触发：翻译论文失败: {exc}")
    finally:
        set_settings_task_running("translate_papers", False)


def run_daily_update_scheduler(username: str):
    """后台循环：每小时检查一次今日论文状态，并按需补齐。"""
    print("自动更新调度器已启动：每 1 小时检查一次今日拉取/翻译状态。")
    while True:
        try:
            with use_active_username(username):
                ensure_today_papers_ready(datetime.now())
        except Exception as exc:
            print(f"自动更新调度器检查失败: {exc}")
        sleep(AUTO_UPDATE_CHECK_INTERVAL_SECONDS)


def start_daily_update_scheduler():
    global AUTO_UPDATE_SCHEDULER_STARTED

    with AUTO_UPDATE_SCHEDULER_LOCK:
        if AUTO_UPDATE_SCHEDULER_STARTED:
            return
        AUTO_UPDATE_SCHEDULER_STARTED = True

    Thread(
        target=run_daily_update_scheduler,
        args=(get_current_username(),),
        daemon=True,
    ).start()


def ensure_today_papers_ready(now=None):
    """每次检查时按需补齐今日任务：缺拉取则拉取，缺翻译则翻译。"""
    global AUTO_UPDATE_RUNNING

    now = now or datetime.now()

    with AUTO_UPDATE_TASK_LOCK:
        if AUTO_UPDATE_RUNNING:
            return
        AUTO_UPDATE_RUNNING = True

    today_label = now.strftime("%Y-%m-%d")
    Thread(target=run_today_papers_ready, args=(now, today_label, get_current_username()), daemon=True).start()


def date_label_from_parts(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def build_query_string(base_path="/", date=None, tags=None, view_month=None, path=None, lang=None, paper=None, query=None):
    params = []
    if date:
        params.append(("date", date))
    if tags:
        for tag in tags:
            params.append(("tag", tag))
    if view_month:
        params.append(("month", view_month))
    if path:
        params.append(("path", path))
    if lang:
        params.append(("lang", lang))
    if paper:
        params.append(("paper", paper))
    if query:
        params.append(("query", query))
    query = urlencode(params, doseq=True)
    return f"{base_path}?{query}" if query else base_path


def get_paper_id(paper: dict) -> str:
    return paper.get("url", "")


def get_arxiv_abs_url(pdf_url: str) -> str:
    if "/pdf/" in pdf_url:
        return pdf_url.replace("/pdf/", "/abs/")
    return pdf_url


def get_localized_text(paper: dict, lang: str, field: str) -> str:
    localized_field = f"{field}_zh"
    if lang == "zh":
        return paper.get(localized_field) or paper.get(field, "")
    return paper.get(field) or paper.get(localized_field, "")


def get_summary_question() -> str:
    return (get("summary.user_question", "") or "").strip()


def get_analysis_job_key(paper_url: str) -> str:
    return (paper_url or "").strip()


def is_analysis_generating(paper_url: str) -> bool:
    job_key = get_analysis_job_key(paper_url)
    if not job_key:
        return False
    with ANALYSIS_JOB_LOCK:
        return bool(ANALYSIS_JOB_STATUS.get(job_key))


def set_analysis_generating(paper_url: str, generating: bool):
    job_key = get_analysis_job_key(paper_url)
    if not job_key:
        return
    with ANALYSIS_JOB_LOCK:
        if generating:
            ANALYSIS_JOB_STATUS[job_key] = True
        else:
            ANALYSIS_JOB_STATUS.pop(job_key, None)


def get_summary_job_key(json_path: Optional[Path]) -> str:
    if not json_path:
        return ""
    return str(json_path.parent / "summary_response.json")


def is_summary_generating(json_path: Optional[Path]) -> bool:
    job_key = get_summary_job_key(json_path)
    if not job_key:
        return False
    with SUMMARY_JOB_LOCK:
        return bool(SUMMARY_JOB_STATUS.get(job_key))


def set_summary_generating(json_path: Optional[Path], generating: bool):
    job_key = get_summary_job_key(json_path)
    if not job_key:
        return
    with SUMMARY_JOB_LOCK:
        if generating:
            SUMMARY_JOB_STATUS[job_key] = True
        else:
            SUMMARY_JOB_STATUS.pop(job_key, None)


def run_summary_generation(source_path: Path, username: str):
    set_summary_generating(source_path, True)
    try:
        with use_active_username(username):
            summary_all_papers(path=str(source_path))
    except Exception as exc:
        print(f"生成今日总结失败: {exc}")
    finally:
        summary_path = source_path.parent / "summary_response.json"
        with SUMMARY_CACHE_LOCK:
            SUMMARY_CACHE.pop(str(summary_path), None)
        set_summary_generating(source_path, False)


def run_analysis_generation(source_path: Path, paper_url: str, username: str):
    set_analysis_generating(paper_url, True)
    try:
        with use_active_username(username):
            analysis_paper(paper_url, str(source_path))
    except Exception as exc:
        print(f"生成论文分析失败: {exc}")
    finally:
        analysis_path = get_analysis_output_path(paper_url)
        with ANALYSIS_CACHE_LOCK:
            ANALYSIS_CACHE.pop(str(analysis_path), None)
        set_analysis_generating(paper_url, False)


def format_summary_inline_markdown(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def render_basic_markdown(markdown_text: str) -> str:
    """Markdown 依赖不可用时的轻量兜底渲染。"""
    if not markdown_text:
        return ""

    normalized_text = markdown_text.replace("\r\n", "\n").strip()
    lines = normalized_text.splitlines()
    html_parts = []
    paragraph_lines = []
    list_type = None
    code_block_lines = []
    code_block_lang = ""
    quote_lines = []

    def flush_paragraph():
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        merged_text = " ".join(line.strip() for line in paragraph_lines if line.strip())
        content = format_summary_inline_markdown(merged_text)
        html_parts.append(f"<p>{content}</p>")
        paragraph_lines = []

    def close_list():
        nonlocal list_type
        if list_type:
            html_parts.append(f"</{list_type}>")
            list_type = None

    def flush_code_block():
        nonlocal code_block_lines, code_block_lang
        if not code_block_lines:
            return
        language_attr = f' data-lang="{html.escape(code_block_lang, quote=True)}"' if code_block_lang else ""
        content = html.escape("\n".join(code_block_lines), quote=False)
        html_parts.append(f"<pre{language_attr}><code>{content}</code></pre>")
        code_block_lines = []
        code_block_lang = ""

    def flush_quote():
        nonlocal quote_lines
        if not quote_lines:
            return
        merged_text = " ".join(line.strip() for line in quote_lines if line.strip())
        content = format_summary_inline_markdown(merged_text)
        html_parts.append(f"<blockquote><p>{content}</p></blockquote>")
        quote_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        fence_match = re.match(r"^```([\w+-]*)\s*$", stripped)
        if fence_match:
            flush_paragraph()
            close_list()
            flush_quote()
            if code_block_lines:
                flush_code_block()
            else:
                code_block_lang = fence_match.group(1).strip()
            continue

        if code_block_lines or code_block_lang:
            code_block_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            close_list()
            flush_quote()
            continue

        if re.match(r"^([-*_])\1{2,}\s*$", stripped):
            flush_paragraph()
            close_list()
            flush_quote()
            html_parts.append("<hr>")
            continue

        quote_match = re.match(r"^>\s?(.*)$", stripped)
        if quote_match:
            flush_paragraph()
            close_list()
            quote_lines.append(quote_match.group(1))
            continue
        flush_quote()

        heading_match = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            close_list()
            level = len(heading_match.group(1)) + 1
            title = format_summary_inline_markdown(heading_match.group(2).strip())
            html_parts.append(f'<h{level} class="summary-heading">{title}</h{level}>')
            continue

        bullet_match = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        ordered_match = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
        if bullet_match or ordered_match:
            flush_paragraph()
            current_type = "ul" if bullet_match else "ol"
            match = bullet_match or ordered_match
            indent_level = min(len(match.group(1)) // 2, 3)
            if list_type != current_type:
                close_list()
                html_parts.append(f"<{current_type}>")
                list_type = current_type
            item = format_summary_inline_markdown(match.group(2).strip())
            html_parts.append(f'<li class="indent-{indent_level}">{item}</li>')
            continue

        close_list()
        paragraph_lines.append(stripped)

    flush_paragraph()
    close_list()
    flush_quote()
    flush_code_block()
    return "".join(html_parts)


def enhance_markdown_html(rendered_html: str) -> str:
    rendered_html = re.sub(
        r'<a href="(https?://[^"]+)"',
        r'<a href="\1" target="_blank" rel="noopener"',
        rendered_html,
    )
    return rendered_html


def render_summary_markdown(markdown_text: str) -> str:
    """优先使用 python-markdown 渲染，缺失时回退到轻量渲染。"""
    if not markdown_text:
        return ""

    normalized_text = markdown_text.replace("\r\n", "\n").strip()
    if not normalized_text:
        return ""

    if python_markdown is None:
        return render_basic_markdown(normalized_text)

    rendered_html = python_markdown.markdown(
        normalized_text,
        extensions=[
            "extra",
            "sane_lists",
            "toc",
        ],
        output_format="html5",
    )
    return enhance_markdown_html(rendered_html)


def load_summary_response(json_path: Optional[Path]):
    """读取与论文数据同目录下的 summary_response.json。"""
    if not json_path:
        return None

    summary_path = json_path.parent / "summary_response.json"
    if not summary_path.exists():
        return None
    summary_mtime = get_file_mtime(summary_path)
    if summary_mtime is None:
        return None

    cache_key = str(summary_path)
    with SUMMARY_CACHE_LOCK:
        cached = SUMMARY_CACHE.get(cache_key)
        if cached and cached["mtime"] == summary_mtime:
            return cached["data"]

    summary_data = read_json_file(summary_path)
    if summary_data is None:
        return None

    response_text = (summary_data.get("response") or "").strip()
    rendered = {
        "path": str(summary_path),
        "user_question": (summary_data.get("user_question") or "").strip(),
        "generated_at": (summary_data.get("generated_at") or "").strip(),
        "response": response_text,
        "response_html": render_summary_markdown(response_text),
    }
    with SUMMARY_CACHE_LOCK:
        SUMMARY_CACHE[cache_key] = {"mtime": summary_mtime, "data": rendered}
    return rendered


def load_analysis_response(paper_url: str):
    if not paper_url:
        return None

    analysis_path = get_analysis_output_path(paper_url)
    if not analysis_path.exists():
        return None
    analysis_mtime = get_file_mtime(analysis_path)
    if analysis_mtime is None:
        return None

    cache_key = str(analysis_path)
    with ANALYSIS_CACHE_LOCK:
        cached = ANALYSIS_CACHE.get(cache_key)
        if cached and cached["mtime"] == analysis_mtime:
            return cached["data"]

    analysis_data = read_json_file(analysis_path)
    if analysis_data is None:
        return None

    response_text = (analysis_data.get("response") or "").strip()
    rendered = {
        "path": str(analysis_path),
        "paper_url": (analysis_data.get("paper_url") or "").strip(),
        "generated_at": (analysis_data.get("generated_at") or "").strip(),
        "response": response_text,
        "response_html": render_summary_markdown(response_text),
    }
    with ANALYSIS_CACHE_LOCK:
        ANALYSIS_CACHE[cache_key] = {"mtime": analysis_mtime, "data": rendered}
    return rendered


def build_summary_status(summary_response, expected_question: str, is_generating: bool):
    saved_question = (summary_response or {}).get("user_question", "").strip() if summary_response else ""
    response_text = (summary_response or {}).get("response", "").strip() if summary_response else ""
    has_summary = bool(summary_response)
    has_response = bool(response_text)
    question_matches = bool(expected_question) and saved_question == expected_question
    if not expected_question:
        question_matches = not saved_question

    needs_generation = (not has_summary) or (not has_response) or (not question_matches)
    reason = "正在生成" if is_generating else ("生成总结" if needs_generation else "已是最新")

    return {
        "has_summary": has_summary,
        "has_response": has_response,
        "question_matches": question_matches,
        "needs_generation": needs_generation,
        "reason": reason,
        "expected_question": expected_question,
        "is_generating": is_generating,
    }


def build_analysis_status(analysis_response, is_generating: bool):
    response_text = (analysis_response or {}).get("response", "").strip() if analysis_response else ""
    has_analysis = bool(analysis_response)
    has_response = bool(response_text)
    needs_generation = (not has_analysis) or (not has_response)
    reason = "正在分析" if is_generating else ("生成分析" if needs_generation else "已生成")
    return {
        "has_analysis": has_analysis,
        "has_response": has_response,
        "needs_generation": needs_generation,
        "reason": reason,
        "is_generating": is_generating,
    }


def build_related_papers(papers, selected_paper, selected_date, selected_tags, view_month, source_path, lang, search_query=""):
    if not selected_paper:
        return []

    active_topics = [tag for tag in selected_tags if isinstance(tag, str) and tag.strip()]
    if not active_topics:
        active_topics = [
            tag for tag in selected_paper.get("topics_zh", [])
            if isinstance(tag, str) and tag.strip()
        ]
    if not active_topics:
        return []

    related = []
    for paper in papers:
        paper_topics = paper.get("_topic_set")
        if paper_topics is None:
            paper_topics = {
                tag for tag in paper.get("topics_zh", [])
                if isinstance(tag, str) and tag.strip()
            }
        if not all(tag in paper_topics for tag in active_topics):
            continue
        related.append(
            {
                "title": get_localized_text(paper, lang, "title"),
                "active": get_paper_id(paper) == get_paper_id(selected_paper),
                "href": build_query_string(
                    date=selected_date,
                    tags=selected_tags,
                    view_month=view_month,
                    path=source_path,
                    lang=lang,
                    paper=get_paper_id(paper),
                    query=search_query,
                ),
            }
        )
    return related


def build_tag_filter_data(
    all_tags,
    selected_tags,
    selected_date,
    source_path,
    view_month,
    lang,
    selected_paper=None,
    search_query="",
):
    filters = [
        {
            "name": "全部",
            "active": not selected_tags,
            "href": build_query_string(
                date=selected_date,
                view_month=view_month,
                path=source_path,
                lang=lang,
                paper=selected_paper,
                query=search_query,
            ),
        }
    ]

    for tag in all_tags:
        active = tag in selected_tags
        next_tags = [item for item in selected_tags if item != tag] if active else [*selected_tags, tag]
        filters.append(
            {
                "name": tag,
                "active": active,
                "href": build_query_string(
                    date=selected_date,
                    tags=next_tags,
                    view_month=view_month,
                    path=source_path,
                    lang=lang,
                    paper=selected_paper,
                    query=search_query,
                ),
            }
        )
    return filters


def build_calendar_data(
    date_values,
    selected_date,
    selected_tags=None,
    source_path=None,
    view_month=None,
    lang="zh",
    selected_paper=None,
    search_query="",
):
    """根据可用日期构造月视图日历。"""
    if not date_values:
        return {
            "title": "",
            "weeks": [],
            "prev_year_link": None,
            "next_year_link": None,
            "prev_month_link": None,
            "next_month_link": None,
            "year": None,
            "month": None,
        }

    active_date = selected_date or date_values[0]
    selected_tags = selected_tags or []
    if view_month:
        year, month = [int(part) for part in view_month.split("-")]
    else:
        year, month, _ = [int(part) for part in active_date.split("-")]

    available_dates_by_month = {}
    for date_value in date_values:
        month_key = "-".join(date_value.split("-")[:2])
        available_dates_by_month.setdefault(month_key, set()).add(int(date_value.split("-")[2]))

    month_key = f"{year:04d}-{month:02d}"
    available_days = available_dates_by_month.get(month_key, set())
    month_keys = sorted(available_dates_by_month.keys())
    available_years = sorted({int(key.split("-")[0]) for key in month_keys})
    available_months_in_year = sorted(
        {int(key.split("-")[1]) for key in month_keys if key.startswith(f"{year:04d}-")}
    )

    first_weekday, days_in_month = monthrange(year, month)
    weeks = []
    current_week = [None] * first_weekday

    for day in range(1, days_in_month + 1):
        value = f"{year:04d}-{month:02d}-{day:02d}"
        current_week.append(
            {
                "day": day,
                "value": value,
                "available": day in available_days,
                "active": value == active_date,
                "link": build_query_string(
                    date=value,
                    tags=selected_tags,
                    view_month=month_key,
                    path=source_path,
                    lang=lang,
                    paper=selected_paper,
                    query=search_query,
                ),
            }
        )
        if len(current_week) == 7:
            weeks.append(current_week)
            current_week = []

    if current_week:
        current_week.extend([None] * (7 - len(current_week)))
        weeks.append(current_week)

    prev_year_link = None
    next_year_link = None
    prev_month_link = None
    next_month_link = None

    year_index = available_years.index(year) if year in available_years else -1
    month_index = available_months_in_year.index(month) if month in available_months_in_year else -1

    if year_index > 0:
        prev_year = available_years[year_index - 1]
        target_month = min(month, max(
            int(key.split("-")[1]) for key in month_keys if key.startswith(f"{prev_year:04d}-")
        ))
        prev_year_link = build_query_string(
            date=active_date,
            tags=selected_tags,
            view_month=f"{prev_year:04d}-{target_month:02d}",
            path=source_path,
            lang=lang,
            paper=selected_paper,
            query=search_query,
        )
    if 0 <= year_index < len(available_years) - 1:
        next_year = available_years[year_index + 1]
        target_month = min(month, max(
            int(key.split("-")[1]) for key in month_keys if key.startswith(f"{next_year:04d}-")
        ))
        next_year_link = build_query_string(
            date=active_date,
            tags=selected_tags,
            view_month=f"{next_year:04d}-{target_month:02d}",
            path=source_path,
            lang=lang,
            paper=selected_paper,
            query=search_query,
        )
    if month_index > 0:
        prev_month = available_months_in_year[month_index - 1]
        prev_month_link = build_query_string(
            date=active_date,
            tags=selected_tags,
            view_month=f"{year:04d}-{prev_month:02d}",
            path=source_path,
            lang=lang,
            paper=selected_paper,
            query=search_query,
        )
    if 0 <= month_index < len(available_months_in_year) - 1:
        next_month = available_months_in_year[month_index + 1]
        next_month_link = build_query_string(
            date=active_date,
            tags=selected_tags,
            view_month=f"{year:04d}-{next_month:02d}",
            path=source_path,
            lang=lang,
            paper=selected_paper,
            query=search_query,
        )

    return {
        "title": f"{year} 年 {month} 月",
        "weeks": weeks,
        "prev_year_link": prev_year_link,
        "next_year_link": next_year_link,
        "prev_month_link": prev_month_link,
        "next_month_link": next_month_link,
        "year": year,
        "month": month,
    }


def list_available_dates():
    """列出存在非空论文数据的日期目录。"""
    cache_ttl = int(get("file.list_cache_seconds", 30) or 30)
    now_ts = time()
    if AVAILABLE_DATES_CACHE["value"] is not None and now_ts - AVAILABLE_DATES_CACHE["updated_at"] < cache_ttl:
        return AVAILABLE_DATES_CACHE["value"]

    file_dir = get_file_dir()
    if not file_dir.exists():
        return []

    available_dates = []
    for year_dir in file_dir.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir() or not day_dir.name.isdigit():
                    continue
                if resolve_non_empty_papers_path(day_dir):
                    available_dates.append(
                        {
                            "value": date_label_from_parts(year_dir.name, month_dir.name, day_dir.name),
                            "dir": day_dir,
                        }
                    )

    sorted_dates = sorted(available_dates, key=lambda item: item["value"], reverse=True)
    with AVAILABLE_DATES_CACHE_LOCK:
        AVAILABLE_DATES_CACHE["value"] = sorted_dates
        AVAILABLE_DATES_CACHE["updated_at"] = now_ts
    return sorted_dates


def find_papers_path(selected_date=None):
    """优先查找指定日期的非空数据，未指定时回退到最新非空日期。"""
    available_dates = list_available_dates()
    if not available_dates:
        return None, available_dates, None

    target_dir = None
    target_value = selected_date

    if selected_date:
        for item in available_dates:
            if item["value"] == selected_date:
                target_dir = item["dir"]
                break

    if target_dir is None:
        target_dir = available_dates[0]["dir"]
        target_value = available_dates[0]["value"]

    resolved_path = resolve_non_empty_papers_path(target_dir)
    if resolved_path:
        return resolved_path, available_dates, target_value
    return None, available_dates, target_value


def load_papers(
    path=None,
    selected_date=None,
    selected_tags=None,
    view_month=None,
    lang="zh",
    selected_paper_id=None,
    search_query="",
):
    """加载论文数据，并提供日期/标签筛选所需元数据。"""
    available_dates = list_available_dates()
    resolved_date = selected_date
    selected_tags = [tag for tag in (selected_tags or []) if tag]
    search_query = (search_query or "").strip()
    collected_urls = get_collected_url_set()

    if selected_date:
        json_path, available_dates, resolved_date = find_papers_path(selected_date)
    elif path:
        json_path = Path(path)
    else:
        json_path, available_dates, resolved_date = find_papers_path(selected_date)
    page_data = build_base_page_data(lang, search_query, "home")
    empty_result = {
        **page_data,
        "total_num": 0,
        "papers": [],
        "all_tags": [],
        "selected_date": resolved_date,
        "available_dates": [item["value"] for item in available_dates],
        "calendar_data": build_calendar_data(
            [item["value"] for item in available_dates],
            resolved_date,
            selected_tags=selected_tags,
            source_path=str(json_path) if path else "",
            view_month=view_month,
            lang=lang,
            selected_paper=selected_paper_id,
            search_query=search_query,
        ),
        "selected_tags": selected_tags,
        "tag_filters": [],
        "selected_paper": None,
        "related_papers": [],
        "source_path": str(json_path) if path else "",
    }

    if not json_path or not json_path.exists():
        return empty_result

    metadata = get_cached_papers_metadata(json_path)
    if metadata is None:
        return empty_result

    summary_response = load_summary_response(json_path)
    summary_status = build_summary_status(
        summary_response,
        page_data["summary_question"],
        is_summary_generating(json_path),
    )

    papers = metadata["papers"]
    all_tags = metadata["all_tags"]

    if selected_tags:
        papers = [
            paper for paper in papers
            if all(tag in paper.get("_topic_set", set()) for tag in selected_tags)
        ]

    selected_paper = None
    if selected_paper_id:
        selected_paper = metadata["paper_index"].get(selected_paper_id)
        if selected_paper and selected_paper not in papers:
            selected_paper = None

    analysis_response = None
    analysis_status = build_analysis_status(None, False)
    if selected_paper:
        paper_url = get_paper_id(selected_paper)
        analysis_response = load_analysis_response(paper_url)
        analysis_status = build_analysis_status(
            analysis_response,
            is_analysis_generating(paper_url),
        )

    view_month_value = view_month or (resolved_date[:7] if resolved_date else None)
    related_papers = build_related_papers(
        papers,
        selected_paper,
        resolved_date,
        selected_tags,
        view_month_value,
        str(json_path),
        lang,
        search_query,
    )

    return {
        **page_data,
        "total_num": len(papers),
        "papers": [
            {
                **paper,
                "is_collected": get_paper_id(paper) in collected_urls,
                "detail_href": build_query_string(
                    date=resolved_date,
                    tags=selected_tags,
                    view_month=view_month_value,
                    path=str(json_path),
                    lang=lang,
                    paper=get_paper_id(paper),
                    query=search_query,
                ),
            }
            for paper in papers
        ],
        "all_tags": all_tags,
        "selected_date": resolved_date,
        "available_dates": [item["value"] for item in available_dates],
        "calendar_data": build_calendar_data(
            [item["value"] for item in available_dates],
            resolved_date,
            selected_tags=selected_tags,
            source_path=str(json_path),
            view_month=view_month,
            lang=lang,
            selected_paper=selected_paper_id,
            search_query=search_query,
        ),
        "selected_tags": selected_tags,
        "tag_filters": build_tag_filter_data(
            all_tags,
            selected_tags,
            resolved_date,
            str(json_path),
            view_month_value,
            lang,
            selected_paper_id,
            search_query,
        ),
        "selected_paper": {
            **selected_paper,
            "is_collected": get_paper_id(selected_paper) in collected_urls,
        } if selected_paper else None,
        "related_papers": related_papers,
        "source_path": str(json_path),
        "summary_response": summary_response,
        "summary_status": summary_status,
        "analysis_response": analysis_response,
        "analysis_status": analysis_status,
    }


def load_favorite_papers(selected_tags=None, lang="zh", search_query=""):
    selected_tags = [tag for tag in (selected_tags or []) if tag]
    search_query = (search_query or "").strip()
    page_data = build_base_page_data(lang, search_query, "favorites")
    stored_papers = load_collections_store()["papers"]

    prepared_papers = []
    all_tags = set()
    for paper in stored_papers:
        topics = [
            tag.strip()
            for tag in paper.get("topics_zh", [])
            if isinstance(tag, str) and tag.strip()
        ]
        all_tags.update(topics)
        prepared_papers.append(
            {
                **paper,
                "_topic_set": set(topics),
            }
        )

    if selected_tags:
        prepared_papers = [
            paper for paper in prepared_papers
            if all(tag in paper.get("_topic_set", set()) for tag in selected_tags)
        ]

    return {
        **page_data,
        "total_num": len(prepared_papers),
        "papers": [
            {
                **paper,
                "is_collected": True,
                "detail_href": build_query_string(
                    date=paper.get("source_date"),
                    path=paper.get("source_path"),
                    lang=lang,
                    paper=get_paper_id(paper),
                    query=search_query,
                ),
            }
            for paper in prepared_papers
        ],
        "all_tags": sorted(all_tags),
        "selected_date": None,
        "available_dates": [],
        "calendar_data": build_calendar_data([], None, selected_tags=selected_tags, lang=lang, search_query=search_query),
        "selected_tags": selected_tags,
        "tag_filters": [
            {
                "name": "全部",
                "active": not selected_tags,
                "href": build_query_string(base_path="/favorites", lang=lang),
            },
            *[
                {
                    "name": tag,
                    "active": tag in selected_tags,
                    "href": build_query_string(
                        base_path="/favorites",
                        tags=[item for item in selected_tags if item != tag] if tag in selected_tags else [*selected_tags, tag],
                        lang=lang,
                        query=search_query,
                    ),
                }
                for tag in sorted(all_tags)
            ],
        ],
        "selected_paper": None,
        "related_papers": [],
        "source_path": "",
    }


@app.route("/api-balance")
def api_balance():
    return {"balance": get_api_balance()}


@app.route("/favorites")
def favorites():
    selected_tags = request.args.getlist("tag")
    if not selected_tags:
        single_tag = request.args.get("tag")
        if single_tag:
            selected_tags = [item for item in single_tag.split(",") if item]
    lang = request.args.get("lang", "zh")
    search_query = request.args.get("query", "")
    if lang not in {"zh", "en"}:
        lang = "zh"
    papers_data = load_favorite_papers(selected_tags=selected_tags, lang=lang, search_query=search_query)
    return render_template("favorites.html", papers_data=papers_data)


@app.route("/settings")
def settings():
    lang = request.args.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"
    return render_template("settings.html", papers_data=build_settings_data(lang))


@app.route("/settings/save", methods=["POST"])
def save_settings():
    lang = request.form.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"

    current_username = get_current_username()
    existing_api_key = str(get("model.api_key", "", username=current_username) or "")
    api_key_input = (request.form.get("model_api_key") or "").strip()
    model_name = (request.form.get("model_model") or "").strip()
    summary_question = (request.form.get("summary_user_question") or "").strip()
    file_area = (request.form.get("file_area") or "").strip()
    file_update_time = (request.form.get("file_update_time") or "").strip()
    ui_debug_raw = (request.form.get("ui_debug") or "").strip().lower()
    draft_form = {
        "model_api_key": api_key_input,
        "model_model": model_name,
        "summary_user_question": summary_question,
        "file_area": file_area,
        "file_update_time": file_update_time,
        "ui_debug": ui_debug_raw in {"1", "true", "yes", "on"},
    }

    if not model_name:
        page_data = build_settings_data(lang)
        page_data["settings_form"] = draft_form
        page_data["settings_error"] = "模型名称不能为空"
        return render_template("settings.html", papers_data=page_data)

    if not file_area:
        page_data = build_settings_data(lang)
        page_data["settings_form"] = draft_form
        page_data["settings_error"] = "主题目录不能为空"
        return render_template("settings.html", papers_data=page_data)

    update_time_match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", file_update_time)
    if not update_time_match:
        page_data = build_settings_data(lang)
        page_data["settings_form"] = draft_form
        page_data["settings_error"] = "更新时间格式必须为 HH:MM，例如 10:00"
        return render_template("settings.html", papers_data=page_data)

    normalized_update_time = f"{int(update_time_match.group(1)):02d}:{int(update_time_match.group(2)):02d}"
    ui_debug = ui_debug_raw in {"1", "true", "yes", "on"}
    final_api_key = api_key_input or existing_api_key

    try:
        update_user_config(
            current_username,
            {
                "model.api_key": final_api_key,
                "model.model": model_name,
                "summary.user_question": summary_question,
                "file.area": file_area,
                "file.update_time": normalized_update_time,
                "ui.debug": ui_debug,
            },
        )
    except Exception as exc:
        page_data = build_settings_data(lang)
        page_data["settings_form"] = draft_form
        page_data["settings_error"] = f"保存失败: {exc}"
        return render_template("settings.html", papers_data=page_data)

    page_data = build_settings_data(lang)
    page_data["settings_notice"] = "设置已保存"
    return render_template("settings.html", papers_data=page_data)


@app.route("/settings/update-papers", methods=["POST"])
def settings_update_papers():
    lang = request.form.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"

    page_data = build_settings_data(lang)
    if is_settings_task_running("update_papers"):
        page_data["settings_notice"] = "论文列表更新任务已在运行，请稍候刷新查看结果。"
        return render_template("settings.html", papers_data=page_data)

    Thread(
        target=run_manual_update_papers,
        args=(get_current_username(),),
        daemon=True,
    ).start()
    page_data["settings_notice"] = "已开始更新论文列表，任务在后台执行。"
    return render_template("settings.html", papers_data=page_data)


@app.route("/settings/translate-papers", methods=["POST"])
def settings_translate_papers():
    lang = request.form.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"

    page_data = build_settings_data(lang)
    if is_settings_task_running("translate_papers"):
        page_data["settings_notice"] = "翻译任务已在运行，请稍候刷新查看结果。"
        return render_template("settings.html", papers_data=page_data)

    Thread(
        target=run_manual_translate_papers,
        args=(get_current_username(),),
        daemon=True,
    ).start()
    page_data["settings_notice"] = "已开始翻译今日论文，任务在后台执行。"
    return render_template("settings.html", papers_data=page_data)


@app.route("/login", methods=["GET", "POST"])
def login():
    lang = request.args.get("lang", "zh") if request.method == "GET" else request.form.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"

    if request.method == "GET":
        redirect_to = resolve_redirect_to(request.args.get("redirect_to"), lang)
        store_auth_feedback(mode="login", open_popup=True)
        return redirect(redirect_to)

    redirect_to = resolve_redirect_to(request.form.get("redirect_to"), lang)
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    try:
        authenticated = authenticate_user(username, password)
    except ValueError as exc:
        store_auth_feedback(error=str(exc), mode="login", open_popup=True)
        return redirect(redirect_to)

    if not authenticated:
        store_auth_feedback(error="用户名或密码错误", mode="login", open_popup=True)
        return redirect(redirect_to)

    session["username"] = username
    clear_auth_feedback()
    return redirect(redirect_to)


@app.route("/register", methods=["POST"])
def register():
    lang = request.form.get("lang", "zh")
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if lang not in {"zh", "en"}:
        lang = "zh"
    redirect_to = resolve_redirect_to(request.form.get("redirect_to"), lang)

    try:
        register_user(username, password)
    except ValueError as exc:
        store_auth_feedback(error=str(exc), mode="login", open_popup=True)
        return redirect(redirect_to)

    session["username"] = username
    clear_auth_feedback()
    return redirect(redirect_to)


@app.route("/logout", methods=["POST"])
def logout():
    lang = request.form.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"
    redirect_to = resolve_redirect_to(request.form.get("redirect_to"), lang)
    session.pop("username", None)
    clear_auth_feedback()
    return redirect(redirect_to)


@app.route("/toggle-collection", methods=["POST"])
def toggle_collection():
    paper_url = (request.form.get("paper_url") or "").strip()
    path = (request.form.get("path") or "").strip()
    redirect_to = (request.form.get("redirect_to") or "").strip() or "/"

    if not paper_url:
        return redirect(redirect_to)

    store = load_collections_store()
    papers = list(store["papers"])
    existing_index = next((index for index, paper in enumerate(papers) if paper.get("url") == paper_url), None)

    if existing_index is not None:
        papers.pop(existing_index)
        save_collections_store(papers)
        return redirect(redirect_to)

    source_path = Path(path) if path else None
    source_paper = find_paper_in_source(source_path, paper_url)
    if source_paper is None:
        return redirect(redirect_to)

    papers.append(build_collection_entry(source_paper, source_path))
    save_collections_store(papers)
    return redirect(redirect_to)


@app.route("/")
def index():
    """主页：论文列表"""
    ensure_today_papers_ready()
    path = request.args.get("path")
    selected_date = request.args.get("date")
    selected_tags = request.args.getlist("tag")
    if not selected_tags:
        single_tag = request.args.get("tag")
        if single_tag:
            selected_tags = [item for item in single_tag.split(",") if item]
    view_month = request.args.get("month")
    lang = request.args.get("lang", "zh")
    selected_paper_id = request.args.get("paper")
    search_query = request.args.get("query", "")
    if lang not in {"zh", "en"}:
        lang = "zh"
    papers_data = load_papers(
        path=path,
        selected_date=selected_date,
        selected_tags=selected_tags,
        view_month=view_month,
        lang=lang,
        selected_paper_id=selected_paper_id,
        search_query=search_query,
    )
    template_name = "paper.html" if papers_data.get("selected_paper") else "index.html"
    return render_template(template_name, papers_data=papers_data)


@app.route("/generate-summary", methods=["POST"])
def generate_summary():
    path = request.form.get("path")
    selected_date = request.form.get("date")
    selected_tags = request.form.getlist("tag")
    view_month = request.form.get("month")
    lang = request.form.get("lang", "zh")
    search_query = request.form.get("query", "")

    redirect_url = build_query_string(
        date=selected_date,
        tags=selected_tags,
        view_month=view_month,
        path=path,
        lang=lang,
        query=search_query,
    )

    if path:
        source_path = Path(path)
    else:
        source_path, _, _ = find_papers_path(selected_date)

    if not source_path or not source_path.exists():
        return redirect(redirect_url)

    if not is_summary_generating(source_path):
        Thread(
            target=run_summary_generation,
            args=(source_path, get_current_username()),
            daemon=True,
        ).start()

    return redirect(redirect_url)


@app.route("/generate-analysis", methods=["POST"])
def generate_analysis():
    path = request.form.get("path")
    selected_date = request.form.get("date")
    selected_tags = request.form.getlist("tag")
    view_month = request.form.get("month")
    lang = request.form.get("lang", "zh")
    search_query = request.form.get("query", "")
    paper_url = (request.form.get("paper_url") or "").strip()

    redirect_url = build_query_string(
        date=selected_date,
        tags=selected_tags,
        view_month=view_month,
        path=path,
        lang=lang,
        paper=paper_url,
        query=search_query,
    )

    if not paper_url:
        return redirect(redirect_url)

    if path:
        source_path = Path(path)
    else:
        source_path, _, _ = find_papers_path(selected_date)

    if not source_path or not source_path.exists():
        return redirect(redirect_url)

    if not is_analysis_generating(paper_url):
        Thread(
            target=run_analysis_generation,
            args=(source_path, paper_url, get_current_username()),
            daemon=True,
        ).start()

    return redirect(redirect_url)


if __name__ == "__main__":
    port = int(get("ui.port") or 5715)
    host = get("ui.host") or "0.0.0.0"
    debug = get("ui.debug", True)
    if (not debug) or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_daily_update_scheduler()
    print(f"论文预览服务启动: http://{host}:{port}")
    print(f"本机访问: http://127.0.0.1:{port}")
    print(f"局域网访问: http://<本机IP>:{port}")
    app.run(host=host, port=port, debug=debug)
