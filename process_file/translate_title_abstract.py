"""
调用 DeepSeek API 翻译 papers.json 中的论文标题和摘要
翻译结果保存到与 papers.json 相同的目录下，文件名为 papers_zh.json
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

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
    
    user_prompt_path = PROMPT_DIR / "translate_title_abstract_user.txt"
    with open(user_prompt_path, "r", encoding="utf-8") as f:
        user_prompt = f.read()
    
    system_prompt_path = PROMPT_DIR / "translate_title_abstract_system.txt"
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


def main():
    api_key = get("model.api_key")
    if not api_key:
        print("请配置 model.api_key")
        sys.exit(1)

    papers_path = find_papers_path(sys.argv[1] if len(sys.argv) > 1 else None)
    if not papers_path or not papers_path.exists():
        print("未找到 papers.json，请指定路径或确保今日目录下有该文件")
        sys.exit(1)

    with open(papers_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    model_api = load_model()
    if not model_api:
        print("模型加载失败")
        sys.exit(1)

    output_dir = papers_path.parent
    output_path = output_dir / "papers_zh.json"

    for i, paper in enumerate(data["papers"]):
        print(f"[{i + 1}/{len(data['papers'])}] 翻译: {paper['title'][:50]}...")
        result = translate_paper(model_api, paper)
        if result:
            paper["title_zh"] = result["title_zh"]
            paper["abstract_zh"] = result["abstract_zh"]
            paper["topics_zh"] = result.get("topics_zh", [])
        else:
            paper["title_zh"] = ""
            paper["abstract_zh"] = ""
            paper["topics_zh"] = []
        time.sleep(0.5)  # 避免请求过快

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    print(f"完成，结果已保存至: {output_path}")


if __name__ == "__main__":
    main()
