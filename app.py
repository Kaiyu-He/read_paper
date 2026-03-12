"""
论文预览网页应用
支持通过 IP 地址访问，展示论文标题、摘要和链接
"""
import json
from calendar import monthrange
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, render_template, request

from config import get, resolve_path

app = Flask(__name__, template_folder="ui")


def get_file_dir():
    save_path = get("file.save_path", "file")
    return resolve_path(save_path)


def date_label_from_parts(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def build_query_string(date=None, tags=None, view_month=None, path=None, lang=None):
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
    query = urlencode(params, doseq=True)
    return f"/?{query}" if query else "/"


def build_tag_filter_data(all_tags, selected_tags, selected_date, source_path, view_month, lang):
    filters = [
        {
            "name": "全部",
            "active": not selected_tags,
            "href": build_query_string(
                date=selected_date,
                view_month=view_month,
                path=source_path,
                lang=lang,
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
                ),
            }
        )
    return filters


def build_calendar_data(date_values, selected_date, selected_tags=None, source_path=None, view_month=None, lang="zh"):
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
        )
    if month_index > 0:
        prev_month = available_months_in_year[month_index - 1]
        prev_month_link = build_query_string(
            date=active_date,
            tags=selected_tags,
            view_month=f"{year:04d}-{prev_month:02d}",
            path=source_path,
            lang=lang,
        )
    if 0 <= month_index < len(available_months_in_year) - 1:
        next_month = available_months_in_year[month_index + 1]
        next_month_link = build_query_string(
            date=active_date,
            tags=selected_tags,
            view_month=f"{year:04d}-{next_month:02d}",
            path=source_path,
            lang=lang,
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
    """优先查找指定日期的数据，其次查找今天，最后回退到最新日期。"""
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
        today = datetime.now().strftime("%Y-%m-%d")
        for item in available_dates:
            if item["value"] == today:
                target_dir = item["dir"]
                target_value = item["value"]
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


def load_papers(path=None, selected_date=None, selected_tags=None, view_month=None, lang="zh"):
    """加载论文数据，并提供日期/标签筛选所需元数据。"""
    available_dates = list_available_dates()
    resolved_date = selected_date
    selected_tags = [tag for tag in (selected_tags or []) if tag]

    if path:
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
        ),
        "selected_tags": selected_tags,
        "tag_filters": [],
        "lang": lang,
        "source_path": str(json_path) if path else "",
    }

    if not json_path or not json_path.exists():
        return empty_result

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return empty_result

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
            if any(tag in paper.get("topics_zh", []) for tag in selected_tags)
        ]

    return {
        "total_num": len(papers),
        "papers": papers,
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
        ),
        "selected_tags": selected_tags,
        "tag_filters": build_tag_filter_data(
            all_tags,
            selected_tags,
            resolved_date,
            str(json_path),
            view_month or (resolved_date[:7] if resolved_date else None),
            lang,
        ),
        "lang": lang,
        "source_path": str(json_path),
    }


@app.route("/")
def index():
    """主页：论文列表"""
    path = request.args.get("path")
    selected_date = request.args.get("date")
    selected_tags = request.args.getlist("tag")
    if not selected_tags:
        single_tag = request.args.get("tag")
        if single_tag:
            selected_tags = [item for item in single_tag.split(",") if item]
    view_month = request.args.get("month")
    lang = request.args.get("lang", "zh")
    if lang not in {"zh", "en"}:
        lang = "zh"
    papers_data = load_papers(
        path=path,
        selected_date=selected_date,
        selected_tags=selected_tags,
        view_month=view_month,
        lang=lang,
    )
    return render_template("index.html", papers_data=papers_data)


if __name__ == "__main__":
    port = int(get("ui.port") or 5715)
    host = get("ui.host") or "0.0.0.0"
    debug = get("ui.debug", True)
    print(f"论文预览服务启动: http://{host}:{port}")
    print(f"本机访问: http://127.0.0.1:{port}")
    print(f"局域网访问: http://<本机IP>:{port}")
    app.run(host=host, port=port, debug=debug)
