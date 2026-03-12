from urllib.request import urlopen
from bs4 import BeautifulSoup
import re
import os
import json
from datetime import datetime

from config import get, resolve_path

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
 
def download_papers_today(
    save_path=None,  # 默认从 config 读取
    area=None,       # 默认从 config 读取
):
    save_path = save_path or str(resolve_path(get("file.save_path", "file")))
    area = area or get("file.area", "RO")
    html = urlopen(f"https://arxiv.org/list/cs.{area}/new")
    bsObj = BeautifulSoup(html, "lxml")
    today = datetime.now()
    year = today.year
    month = today.month
    date = today.day
    path = os.path.join(save_path, str(year), str(month), str(date))

    total_size = 0
    os.makedirs(path, exist_ok=True)

    titleList = bsObj.findAll("div", {"class":"list-title mathjax"})
    papers = {
        "total_num": 0,
        "papers": []
    }
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
            print(os.path.join(path, 'papers.json'))
            with open(os.path.join(path, 'papers.json'), 'w') as f:
                json.dump(papers, f, indent=4)

    print(f"{year}年{month}月{date}日发布的论文已载入完成")


if __name__ == "__main__":
    download_papers_today()  # 从 config/hekaiyu.yaml 读取 save_path、area
