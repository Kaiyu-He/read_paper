"""
调用 DeepSeek API 翻译 papers.json 中的论文标题和摘要
翻译结果保存到与 papers.json 相同的目录下，文件名为 papers_zh.json
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import get, resolve_path
from model.api import load_model

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = BASE_DIR / "prompt"


def find_papers_path(path=None):
    """查找 papers.json 路径"""
    if path:
        return Path(path)
    save_path = get("file.save_path")
    file_dir = resolve_path(save_path)
    
    today = datetime.now()
    json_path = file_dir / str(today.year) / str(today.month) / str(today.day) / "papers.json"
    if json_path.exists():
        return json_path
    # 今日目录不存在时，使用最新的 papers.json
    papers_files = list(file_dir.rglob("papers.json"))
    return max(papers_files, key=lambda p: p.stat().st_mtime) if papers_files else None


def resolve_translation_paths(path=None):
    """解析输入路径，返回英文源文件和中文输出文件路径。"""
    selected_path = find_papers_path(path)
    if not selected_path:
        return None, None

    if selected_path.name == "papers_zh.json":
        output_path = selected_path
        source_path = selected_path.with_name("papers.json")
        if not source_path.exists():
            source_path = selected_path
        return source_path, output_path

    if selected_path.name == "papers.json":
        return selected_path, selected_path.with_name("papers_zh.json")

    return selected_path, selected_path.parent / "papers_zh.json"


def get_paper_key(paper: dict) -> str:
    """优先使用 url 作为论文唯一键，否则退回标题。"""
    return (paper.get("url") or paper.get("title") or "").strip()


def is_missing_translation(paper: dict) -> bool:
    """判断论文是否仍有未翻译字段。"""
    title_zh = (paper.get("title_zh") or "").strip()
    abstract_zh = (paper.get("abstract_zh") or "").strip()
    topics_zh = [tag for tag in (paper.get("topics_zh") or []) if isinstance(tag, str) and tag.strip()]
    return not title_zh or not abstract_zh or not topics_zh


def load_json_file(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def merge_existing_translations(source_data: dict, existing_data: Optional[dict]):
    """将已有翻译结果合并到英文源数据中，用于断点续翻。"""
    papers = source_data.get("papers", [])
    existing_map = {}

    if existing_data:
        for paper in existing_data.get("papers", []):
            key = get_paper_key(paper)
            if key:
                existing_map[key] = paper

    merged_papers = []
    for paper in papers:
        merged_paper = dict(paper)
        existing_paper = existing_map.get(get_paper_key(paper))
        if existing_paper:
            for field in ("title_zh", "abstract_zh", "topics_zh"):
                if field in existing_paper:
                    merged_paper[field] = existing_paper[field]
        merged_paper.setdefault("title_zh", "")
        merged_paper.setdefault("abstract_zh", "")
        merged_paper.setdefault("topics_zh", [])
        merged_papers.append(merged_paper)

    merged_data = dict(source_data)
    merged_data["papers"] = merged_papers
    merged_data["total_num"] = len(merged_papers)
    return merged_data


def get_pending_translation_count(source_path: Path, output_path: Path) -> int:
    """返回仍需翻译的论文数量。"""
    source_data = load_json_file(source_path)
    existing_data = load_json_file(output_path) if output_path.exists() else None
    data = merge_existing_translations(source_data, existing_data)
    return sum(1 for paper in data.get("papers", []) if is_missing_translation(paper))

def parse_translation_response(text: str):
    """从模型响应中解析 JSON"""
    # 尝试提取 ```json ... ``` 块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def translate_paper(model_api, paper: dict):
    """调用 API 翻译单篇论文"""
    tags_path = PROMPT_DIR / "RO" / "tags"
    with open(tags_path, "r", encoding="utf-8") as f:
        tags = f.read().strip().rstrip("、")
    
    user_prompt_path = PROMPT_DIR / "translate" / "translate.txt"
    with open(user_prompt_path, "r", encoding="utf-8") as f:
        user_prompt = f.read()
    
    system_prompt_path = PROMPT_DIR / "translate" / "system.txt"
    with open(system_prompt_path, "r", encoding="utf-8") as f:
        system_prompt = f.read()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt.replace("<|title|>", paper["title"]).replace("<|abstract|>", paper.get("abstract", "")).replace("<|tags|>", tags or "")},
    ]
    try:
        result = model_api.inference(messages)
        if not result:
            return None
        return parse_translation_response(result)
    except Exception as e:
        print(f"翻译失败: {e}")
        return None


def translate_papers(path=None):
    """翻译指定或当日的论文数据，支持断点续翻。"""
    api_key = get("model.api_key")
    if not api_key:
        raise RuntimeError("请配置 model.api_key")

    source_path, output_path = resolve_translation_paths(path)
    if not source_path or not source_path.exists():
        raise FileNotFoundError("未找到论文数据文件，请指定 papers.json 或 papers_zh.json 路径")

    source_data = load_json_file(source_path)
    existing_data = load_json_file(output_path) if output_path.exists() else None
    data = merge_existing_translations(source_data, existing_data)

    pending_count = sum(1 for paper in data.get("papers", []) if is_missing_translation(paper))
    if pending_count == 0:
        save_json_file(output_path, data)
        print(f"无需翻译，已全部完成: {output_path}")
        return output_path

    model_api = load_model()
    if not model_api:
        raise RuntimeError("模型加载失败")

    print(f"待补全翻译: {pending_count} / {len(data.get('papers', []))}")

    for i, paper in enumerate(data["papers"]):
        if not is_missing_translation(paper):
            print(f"[{i + 1}/{len(data['papers'])}] 跳过: {paper['title'][:50]}...")
            continue

        print(f"[{i + 1}/{len(data['papers'])}] 翻译: {paper['title'][:50]}...")
        result = translate_paper(model_api, paper)
        if result:
            paper["title_zh"] = result.get("title_zh", "")
            paper["abstract_zh"] = result.get("abstract_zh", "")
            paper["topics_zh"] = result.get("topics_zh", [])
        else:
            paper.setdefault("title_zh", "")
            paper.setdefault("abstract_zh", "")
            paper.setdefault("topics_zh", [])
        time.sleep(0.5)  # 避免请求过快

        save_json_file(output_path, data)

    print(f"完成，结果已保存至: {output_path}")
    return output_path


def main():
    try:
        translate_papers(sys.argv[1] if len(sys.argv) > 1 else None)
    except (FileNotFoundError, RuntimeError) as exc:
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
