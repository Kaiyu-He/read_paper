"""
基于单篇论文 PDF、标题、摘要和标签生成 AI 辅助阅读分析。
默认输出到 file/analysis/<paper-hash>.json，并缓存下载的 PDF 到 file/analysis/pdf/。
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = BASE_DIR / "prompt" / "analysis"


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


def resolve_source_path(path: str) -> Path:
    source_path = Path(path)
    if not source_path.is_absolute():
        source_path = resolve_project_path(str(source_path))
    if source_path.name == "papers.json":
        zh_path = source_path.with_name("papers_zh.json")
        if zh_path.exists():
            return zh_path
    return source_path


def get_analysis_dir() -> Path:
    return resolve_project_path("file/analysis")


def get_analysis_filename(paper_url: str, suffix: str = ".json") -> str:
    digest = hashlib.sha1((paper_url or "").strip().encode("utf-8")).hexdigest()
    return f"{digest}{suffix}"


def get_analysis_output_path(paper_url: str, output_path=None) -> Path:
    if output_path:
        candidate = Path(output_path)
        if not candidate.is_absolute():
            candidate = resolve_project_path(str(candidate))
        if candidate.exists() and candidate.is_dir():
            return candidate / get_analysis_filename(paper_url)
        if not candidate.suffix:
            return candidate / get_analysis_filename(paper_url)
        return candidate
    return get_analysis_dir() / get_analysis_filename(paper_url)


def get_analysis_pdf_path(paper_url: str) -> Path:
    return get_analysis_dir() / "pdf" / get_analysis_filename(paper_url, suffix=".pdf")


def find_paper(source_path: Path, paper_url: str):
    data = load_json_file(source_path)
    for paper in data.get("papers", []):
        if (paper.get("url") or "").strip() == paper_url:
            return paper
    return None


def normalize_pdf_url(paper_url: str) -> str:
    url = (paper_url or "").strip()
    if not url:
        return url

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if "arxiv.org" in host:
        match = re.search(r"/abs/([^/?#]+)", path)
        if match:
            paper_id = match.group(1).strip()
            return f"https://arxiv.org/pdf/{paper_id}.pdf"
        match = re.search(r"/pdf/([^/?#]+)", path)
        if match:
            paper_id = match.group(1).strip()
            if not paper_id.endswith(".pdf"):
                paper_id = f"{paper_id}.pdf"
            return f"https://arxiv.org/pdf/{paper_id}"
    return url


def download_pdf(paper_url: str, target_path: Path) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    normalized_url = normalize_pdf_url(paper_url)
    last_error = None
    for attempt in range(1, 4):
        try:
            request = Request(
                normalized_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36",
                    "Accept": "application/pdf,*/*;q=0.8",
                    "Connection": "close",
                },
            )
            with urlopen(request, timeout=120) as response:
                data = response.read()
            if not data:
                raise RuntimeError("下载到空 PDF 内容")
            target_path.write_bytes(data)
            return target_path
        except (HTTPError, URLError, TimeoutError, ConnectionResetError, RuntimeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * attempt)
            continue

    raise RuntimeError(f"PDF 下载失败: {last_error}")


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        from model.read_pdf import get_pdf_text
        text = get_pdf_text(str(pdf_path))
    except Exception as exc:
        raise RuntimeError(f"PDF 解析失败: {exc}") from exc
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if not normalized:
        raise RuntimeError("PDF 文本为空，无法生成论文分析")
    return normalized


def build_messages(paper: dict, paper_text: str):
    system_prompt = load_text_file(PROMPT_DIR / "system.txt")
    user_prompt = load_text_file(PROMPT_DIR / "user.txt")
    user_content = (
        user_prompt
        .replace("<|title|>", (paper.get("title") or "").strip())
        .replace("<|abstract|>", (paper.get("abstract") or "").strip())
        .replace("<|paper|>", paper_text.strip())
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def analysis_paper(paper_url: str, path: str, output_path=None):
    paper_url = (paper_url or "").strip()
    if not paper_url:
        raise ValueError("请提供 paper_url")

    if not path:
        raise ValueError("请提供论文数据路径 path")

    if not get_config_value("model.api_key"):
        raise RuntimeError("请配置 model.api_key")

    source_path = resolve_source_path(path)
    if not source_path.exists():
        raise FileNotFoundError(f"未找到论文数据文件: {source_path}")

    paper = find_paper(source_path, paper_url)
    if paper is None:
        raise FileNotFoundError("在论文数据中未找到对应论文")

    pdf_path = download_pdf(paper_url, get_analysis_pdf_path(paper_url))
    paper_text = extract_pdf_text(pdf_path)

    model_api = get_model()
    if not model_api:
        raise RuntimeError("模型加载失败")

    result = model_api.inference(build_messages(paper, paper_text))
    if not result:
        raise RuntimeError("论文分析生成失败")

    target_output_path = get_analysis_output_path(paper_url, output_path=output_path)
    payload = {
        "paper_url": paper_url,
        "title": (paper.get("title") or "").strip(),
        "title_zh": (paper.get("title_zh") or "").strip(),
        "source_path": str(source_path),
        "pdf_path": str(pdf_path),
        "model": get_config_value("model.model", ""),
        "generated_at": datetime.now().isoformat(),
        "response": result.strip(),
    }
    save_json_file(target_output_path, payload)
    print(f"论文分析已保存至: {target_output_path}")
    return target_output_path


def parse_args():
    parser = argparse.ArgumentParser(description="对单篇论文生成 AI 辅助阅读分析")
    parser.add_argument("--paper-url", required=True, help="论文 PDF URL")
    parser.add_argument("--path", required=True, help="papers.json 或 papers_zh.json 路径")
    parser.add_argument("--output", "-o", help="指定输出 JSON 文件路径或输出目录")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        analysis_paper(args.paper_url, args.path, output_path=args.output)
    except (ValueError, FileNotFoundError, RuntimeError, URLError) as exc:
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
