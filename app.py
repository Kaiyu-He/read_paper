"""
论文预览网页应用
支持通过 IP 地址访问，展示论文标题、摘要和链接
"""
import html
import json
import re
from calendar import monthrange
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from time import time
from typing import Optional
from urllib.parse import urlencode

from flask import Flask, redirect, render_template, request

from config import get, resolve_path
from model.api import load_model
from process_file.load_papers import download_papers_today
from process_file.summary_all_papers import summary_all_papers
from process_file.translate_title_abstract import get_pending_translation_count, translate_papers

app = Flask(__name__, template_folder="ui")
AUTO_LOAD_LOCK = Lock()
AUTO_LOAD_DONE_DATE = None
BALANCE_CACHE_LOCK = Lock()
BALANCE_CACHE = {"value": None, "updated_at": 0.0}
SUMMARY_JOB_LOCK = Lock()
SUMMARY_JOB_STATUS = {}


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


def get_today_dir(now=None):
    now = now or datetime.now()
    return get_file_dir() / str(now.year) / str(now.month) / str(now.day)


def ensure_today_papers_ready(now=None):
    """12 点后如果今日论文未加载，则自动抓取并翻译。"""
    global AUTO_LOAD_DONE_DATE

    now = now or datetime.now()
    if now.hour < 12:
        return

    today_label = now.strftime("%Y-%m-%d")
    if AUTO_LOAD_DONE_DATE == today_label:
        return

    with AUTO_LOAD_LOCK:
        if AUTO_LOAD_DONE_DATE == today_label:
            return

        today_dir = get_today_dir(now)
        papers_path = today_dir / "papers.json"
        translation_path = today_dir / "papers_zh.json"

        try:
            if not papers_path.exists():
                print(f"{today_label} 12点后检测到今日论文未载入，开始自动抓取")
                download_papers_today()

            if papers_path.exists():
                pending_count = get_pending_translation_count(papers_path, translation_path)
                if pending_count > 0:
                    print(f"{today_label} 检测到今日论文待翻译 {pending_count} 篇，开始自动翻译")
                    translate_papers(str(papers_path))

            if papers_path.exists() and get_pending_translation_count(papers_path, translation_path) == 0:
                AUTO_LOAD_DONE_DATE = today_label
        except Exception as exc:
            print(f"今日论文自动补跑失败: {exc}")


def date_label_from_parts(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def build_query_string(date=None, tags=None, view_month=None, path=None, lang=None, paper=None, query=None):
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
    return f"/?{query}" if query else "/"


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


def run_summary_generation(source_path: Path):
    set_summary_generating(source_path, True)
    try:
        summary_all_papers(path=str(source_path))
    except Exception as exc:
        print(f"生成今日总结失败: {exc}")
    finally:
        set_summary_generating(source_path, False)


def format_summary_inline_markdown(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def render_summary_markdown(markdown_text: str) -> str:
    """将总结 Markdown 转为适合页面展示的轻量 HTML。"""
    if not markdown_text:
        return ""

    normalized_text = re.sub(r"\n+", "\n", markdown_text.strip())
    lines = normalized_text.splitlines()
    html_parts = []
    paragraph_lines = []
    list_type = None

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

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            close_list()
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
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
    return "".join(html_parts)


def load_summary_response(json_path: Optional[Path]):
    """读取与论文数据同目录下的 summary_response.json。"""
    if not json_path:
        return None

    summary_path = json_path.parent / "summary_response.json"
    if not summary_path.exists():
        return None

    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            summary_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

    response_text = (summary_data.get("response") or "").strip()

    return {
        "path": str(summary_path),
        "user_question": (summary_data.get("user_question") or "").strip(),
        "generated_at": (summary_data.get("generated_at") or "").strip(),
        "response": response_text,
        "response_html": render_summary_markdown(response_text),
    }


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
    """列出存在 papers.json 或 papers_zh.json 的日期目录。"""
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
                has_data = (day_dir / "papers_zh.json").exists() or (day_dir / "papers.json").exists()
                if has_data:
                    available_dates.append(
                        {
                            "value": date_label_from_parts(year_dir.name, month_dir.name, day_dir.name),
                            "dir": day_dir,
                        }
                    )

    return sorted(available_dates, key=lambda item: item["value"], reverse=True)


def find_papers_path(selected_date=None):
    """优先查找指定日期的数据，未指定时回退到最新日期。"""
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

    preferred = target_dir / "papers_zh.json"
    fallback = target_dir / "papers.json"
    if preferred.exists():
        return preferred, available_dates, target_value
    if fallback.exists():
        return fallback, available_dates, target_value
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
    api_balance = get_api_balance()
    summary_question = get_summary_question()

    if selected_date:
        json_path, available_dates, resolved_date = find_papers_path(selected_date)
    elif path:
        json_path = Path(path)
    else:
        json_path, available_dates, resolved_date = find_papers_path(selected_date)
    empty_result = {
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
        "lang": lang,
        "selected_paper": None,
        "related_papers": [],
        "source_path": str(json_path) if path else "",
        "api_balance": api_balance,
        "summary_response": None,
        "summary_status": build_summary_status(None, summary_question, False),
        "summary_question": summary_question,
        "search_query": search_query,
    }

    if not json_path or not json_path.exists():
        return empty_result

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return empty_result

    summary_response = load_summary_response(json_path)
    summary_status = build_summary_status(
        summary_response,
        summary_question,
        is_summary_generating(json_path),
    )

    papers = data.get("papers", [])
    all_tags = sorted(
        {
            tag.strip()
            for paper in papers
            for tag in paper.get("topics_zh", [])
            if isinstance(tag, str) and tag.strip()
        }
    )

    if selected_tags:
        papers = [
            paper for paper in papers
            if all(tag in paper.get("topics_zh", []) for tag in selected_tags)
        ]

    selected_paper = None
    if selected_paper_id:
        for paper in papers:
            if get_paper_id(paper) == selected_paper_id:
                selected_paper = paper
                break

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
        "total_num": len(papers),
        "papers": [
            {
                **paper,
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
        "lang": lang,
        "selected_paper": selected_paper,
        "related_papers": related_papers,
        "source_path": str(json_path),
        "api_balance": api_balance,
        "summary_response": summary_response,
        "summary_status": summary_status,
        "summary_question": summary_question,
        "search_query": search_query,
    }


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
        Thread(target=run_summary_generation, args=(source_path,), daemon=True).start()

    return redirect(redirect_url)


if __name__ == "__main__":
    port = int(get("ui.port") or 5715)
    host = get("ui.host") or "0.0.0.0"
    debug = get("ui.debug", True)
    print(f"论文预览服务启动: http://{host}:{port}")
    print(f"本机访问: http://127.0.0.1:{port}")
    print(f"局域网访问: http://<本机IP>:{port}")
    app.run(host=host, port=port, debug=debug)
