"""
调用 DeepSeek API，基于今日全部论文和用户关注问题生成总结。
默认优先读取 papers_zh.json，并将结果保存为同目录下的 summary_response.json。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = BASE_DIR / "prompt" / "summary"


def get_config_value(key: str, default=None):
    from config import get
    return get(key, default)


def resolve_project_path(path: str) -> Path:
    from config import resolve_path
    return resolve_path(path)


def get_model():
    from model.api import load_model
    return load_model()


def load_text_file(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_json_file(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def find_latest_papers_path(path=None):
    """优先查找指定路径；否则优先今日 papers_zh.json，再回退到最新论文文件。"""
    if path:
        selected_path = Path(path)
        if selected_path.name == "papers.json":
            zh_path = selected_path.with_name("papers_zh.json")
            return zh_path if zh_path.exists() else selected_path
        return selected_path

    save_path = resolve_project_path(get_config_value("file.save_path", "file"))
    today = datetime.now()
    today_dir = save_path / str(today.year) / str(today.month) / str(today.day)

    preferred_today = [today_dir / "papers_zh.json", today_dir / "papers.json"]
    for candidate in preferred_today:
        if candidate.exists():
            return candidate

    candidates = list(save_path.rglob("papers_zh.json"))
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    candidates = list(save_path.rglob("papers.json"))
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    return None


def resolve_output_path(source_path: Path, output_path=None) -> Path:
    output_file = get_config_value("summary.output_file", "summary_response.json")

    if output_path:
        candidate = Path(output_path)
        if not candidate.is_absolute():
            candidate = resolve_project_path(str(candidate))
        # 允许将 --output 传成目录，此时自动写入 summary_response.json
        if candidate.exists() and candidate.is_dir():
            return candidate / output_file
        if not candidate.suffix:
            return candidate / output_file
        return candidate

    # 默认输出到论文源文件所在的日期目录，例如 file/2026/3/13/summary_response.json
    return source_path.parent / output_file


def get_best_title(paper: dict) -> str:
    return (paper.get("title_zh") or paper.get("title") or "").strip()


def get_best_abstract(paper: dict) -> str:
    return (paper.get("abstract_zh") or paper.get("abstract") or "").strip()


def build_papers_context(data: dict) -> str:
    papers = data.get("papers", [])
    blocks = []

    for index, paper in enumerate(papers, start=1):
        title = (paper.get("title") or "").strip()
        title_zh = (paper.get("title_zh") or "").strip()
        abstract = get_best_abstract(paper)
        topics_zh = [tag.strip() for tag in paper.get("topics_zh", []) if isinstance(tag, str) and tag.strip()]
        url = (paper.get("url") or "").strip()

        block = [
            f"[论文 {index}]",
            f"标题: {title or '无'}",
            f"中文标题: {title_zh or get_best_title(paper) or '无'}",
            f"摘要: {abstract or '无'}",
            f"标签: {'、'.join(topics_zh) if topics_zh else '无'}",
            f"链接: {url or '无'}",
        ]
        blocks.append("\n".join(block))

    return "\n\n".join(blocks)


def build_messages(user_question: str, papers_context: str):
    system_prompt = load_text_file(PROMPT_DIR / "system_summary_papers.txt")
    user_prompt = load_text_file(PROMPT_DIR / "summary_papers.txt")
    user_content = (
        user_prompt
        .replace("<|user_question|>", user_question.strip())
        .replace("<|papers|>", papers_context.strip())
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def summary_all_papers(user_question: str = "", path=None, output_path=None):
    if not (user_question or "").strip():
        user_question = get_config_value("summary.user_question", "")
    if not (user_question or "").strip():
        raise ValueError("请通过 --question 提供用户关注问题，或在 config/hekaiyu.yaml 中设置 summary.user_question")

    if not get_config_value("model.api_key"):
        raise RuntimeError("请配置 model.api_key")

    source_path = find_latest_papers_path(path)
    if not source_path or not source_path.exists():
        raise FileNotFoundError("未找到论文数据文件，请指定 papers.json 或 papers_zh.json 路径")

    data = load_json_file(source_path)
    papers = data.get("papers", [])
    if not papers:
        raise RuntimeError(f"论文列表为空: {source_path}")

    model_api = get_model()
    if not model_api:
        raise RuntimeError("模型加载失败")

    papers_context = build_papers_context(data)
    messages = build_messages(user_question, papers_context)
    result = model_api.inference(messages)
    if not result:
        raise RuntimeError("总结生成失败")

    print("开始推理")
    target_output_path = resolve_output_path(source_path, output_path)
    response_payload = {
        "user_question": user_question.strip(),
        "source_path": str(source_path),
        "paper_count": len(papers),
        "model": get_config_value("model.model", ""),
        "generated_at": datetime.now().isoformat(),
        "response": result.strip(),
    }
    save_json_file(target_output_path, response_payload)
    print(f"总结已保存至: {target_output_path}")
    return target_output_path


def parse_args():
    parser = argparse.ArgumentParser(description="根据今日论文和用户问题生成总结")
    parser.add_argument(
        "--question",
        "-q",
        help="用户关注问题；若不传，则读取 config/hekaiyu.yaml 中的 summary.user_question",
    )
    parser.add_argument(
        "--path",
        "-p",
        help="指定 papers.json 或 papers_zh.json 路径；默认优先读取今日 papers_zh.json",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="指定输出 JSON 文件路径或输出目录；默认写入论文数据所在目录下的 summary_response.json",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        summary_all_papers(args.question, path=args.path, output_path=args.output)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
