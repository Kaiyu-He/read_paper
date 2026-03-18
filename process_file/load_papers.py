from urllib.request import urlopen
from bs4 import BeautifulSoup
import re
import os
import json
import time
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import get, resolve_path

def parse_areas(area_value):
    if isinstance(area_value, list):
        areas = [str(item).strip() for item in area_value if str(item).strip()]
    else:
        areas = [item.strip() for item in str(area_value or "RO").split(",") if item.strip()]
    return areas or ["RO"]


def human_readable_size(size_in_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0

# 判断是否下载pdf文件：
def decide(title, abstract, regular):
    s1 = re.search(regular, str(title.get_text()))
    s2 = re.search(regular, str(abstract.get_text()))
    if s1 is not None:
        return True
    elif s2 is not None:
        return True
    else:
        return False
 
def download_papers_for_area(save_path, area):
    save_path = save_path or str(resolve_path(get("file.save_path", "file")))
    papers = {
        "total_num": 0,
        "papers": []
    }

    html = urlopen(f"https://arxiv.org/list/{area}/new")
    bsObj = BeautifulSoup(html, "lxml")
    bsObj = str(bsObj).split("Replacement submissions")[0]
    bsObj = BeautifulSoup(bsObj, "lxml")
    today = datetime.now()
    dateline = bsObj.find_all("h3")[0]
    public_date = dateline.get_text().split(' ')[-3]

    year = today.year
    month = today.month
    date = today.day
    path = os.path.join(save_path, str(year), str(month), str(date), area)
    total_size = 0
    os.makedirs(path, exist_ok=True)

    if str(date) != public_date:
        print(f"今日为{str(date)}日，arxiv 最新发布日期为 {public_date}日，不更新")
        with open(os.path.join(path, 'papers.json'), 'w') as f:
                json.dump(papers, f, indent=4)
        with open(os.path.join(path, 'papers_zh.json'), 'w') as f:
                json.dump(papers, f, indent=4)
        return -1

    titleList = bsObj.findAll("div", {"class":"list-title mathjax"})
    for title in titleList:
        abstract = title.parent.find("p", {"class":"mathjax"})
        if abstract is not None:
            
            download = title.parent.parent.previous_sibling.previous_sibling.find("a", {"title":"Download PDF"}).attrs['href']
            fileUrl = 'https://arxiv.org' + download
            papers['papers'].append(
                {
                    "title": title.get_text().strip().split("Title:\n          ")[1],
                    "abstract": abstract.get_text().strip(),
                    "url": fileUrl,
                }
            )

            papers['total_num'] = len(papers['papers'])
            papers['total_size'] = human_readable_size(total_size)
            with open(os.path.join(path, 'papers.json'), 'w') as f:
                json.dump(papers, f, indent=4)

    print(f"{year}年{month}月{date}日发布的论文已载入完成, {papers['total_num']}篇")
    return 0


def download_papers_today(
    save_path=None,  # 默认从 config 读取
    area=None,       # 默认从 config 读取，可传逗号分隔或列表
):
    save_path = save_path or str(resolve_path(get("file.save_path", "file")))
    area_value = area if area is not None else get("file.area", "RO")
    areas = parse_areas(area_value)
    status_list = []
    for area_name in areas:
        print(f"开始抓取领域: {area_name}")
        status_list.append(download_papers_for_area(save_path, area_name))
    return 0 if any(status == 0 for status in status_list) else -1

def download_papers_week(
    save_path: None,
    area: None
):
    save_path = save_path or str(resolve_path(get("file.save_path", "file")))
    papers = {
        "total_num": 0,
        "papers": []
    }
    bsObj = ""
    for i in range(4): 
        html = urlopen(f"https://arxiv.org/list/cs.RO/recent?skip={i * 50}&show=50")
        bsObj += str(BeautifulSoup(html, "lxml"))
        
    bsObj = BeautifulSoup(bsObj, "lxml")
    today = datetime.now()
    dateline = bsObj.find_all("h3")
    print(dateline)
    raise
   

    year = today.year
    month = today.month
    date = today.day
    path = os.path.join(save_path, str(year), str(month), str(date), area)
    total_size = 0
    os.makedirs(path, exist_ok=True)

    if str(date) != public_date:
        print(f"今日为{str(date)}日，arxiv 最新发布日期为 {public_date}日，不更新")
        with open(os.path.join(path, 'papers.json'), 'w') as f:
                json.dump(papers, f, indent=4)
        with open(os.path.join(path, 'papers_zh.json'), 'w') as f:
                json.dump(papers, f, indent=4)
        return -1

    titleList = bsObj.findAll("div", {"class":"list-title mathjax"})
    for title in titleList:
        abstract = title.parent.find("p", {"class":"mathjax"})
        if abstract is not None:
            
            download = title.parent.parent.previous_sibling.previous_sibling.find("a", {"title":"Download PDF"}).attrs['href']
            fileUrl = 'https://arxiv.org' + download
            papers['papers'].append(
                {
                    "title": title.get_text().strip().split("Title:\n          ")[1],
                    "abstract": abstract.get_text().strip(),
                    "url": fileUrl,
                }
            )

            papers['total_num'] = len(papers['papers'])
            papers['total_size'] = human_readable_size(total_size)
            with open(os.path.join(path, 'papers.json'), 'w') as f:
                json.dump(papers, f, indent=4)

    print(f"{year}年{month}月{date}日发布的论文已载入完成")
    return 0
if __name__ == "__main__":
    download_papers_week(".","cs.RO")  # 从 config/hekaiyu.yaml 读取 save_path、area
