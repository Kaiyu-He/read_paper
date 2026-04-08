# Read Paper

一个面向 arXiv 论文追踪与阅读的 Flask Web 应用。项目会按配置抓取指定领域的最新论文，生成本地论文列表，并支持标题/摘要翻译、按关注问题汇总、单篇论文分析、收藏和多用户配置。

## 功能简介

- 按 arXiv 分类抓取当日最新论文
- 将论文标题、摘要翻译为中文，并生成中文标签
- 基于全部论文生成每日总结
- 下载单篇 PDF，提取全文后生成 AI 阅读分析
- 提供 Web 页面浏览、搜索、筛选、收藏与设置页
- 支持多用户登录，每个用户使用独立 YAML 配置

## 技术栈

- Python
- Flask
- OpenAI Python SDK
- DeepSeek API
- BeautifulSoup / lxml
- pdfplumber

## 项目结构

```text
read_paper/
├── app.py                         # Flask 入口
├── config/                        # 用户配置与账号信息
├── model/                         # 模型调用与 PDF 读取
├── process_file/                  # 论文抓取、翻译、总结、分析脚本
├── prompt/                        # 各类提示词模板
├── ui/                            # 前端页面模板
├── requirements.txt               # Python 依赖
└── run.sh                         # 启动脚本（当前为本地开发路径示例）
```

## 安装

建议使用 Python 3.10 及以上版本。

```bash
pip install -r requirements.txt
```

## 配置

默认配置文件位于 `config/hekaiyu.yaml`，主要配置项如下：

```yaml
model:
  api_key: your_api_key
  model: deepseek-chat

file:
  save_path: ./file
  area: cs.RO
  update_time: "13:00"

ui:
  host: 0.0.0.0
  port: 5715
  debug: true

summary:
  user_question: 你最关注的问题
```

说明：

- `model.api_key`：用于翻译、汇总和论文分析
- `model.model`：当前代码适配 `deepseek-chat` 和 `deepseek-reasoner`
- `file.save_path`：论文数据落盘目录
- `file.area`：支持单个或多个 arXiv 分类，多个分类可用逗号分隔
- `summary.user_question`：生成每日总结时使用的关注问题

建议不要将真实 API Key 提交到仓库。

## 启动项目

直接运行：

```bash
python app.py
```

启动后访问：

- 本机：`http://127.0.0.1:端口`
- 局域网：`http://<你的IP>:端口`

`run.sh` 里当前写的是作者机器上的绝对路径，如需使用请先改成你自己的项目目录。

## 使用流程

1. 配置 `config/*.yaml` 中的模型参数、论文存储目录和 arXiv 领域。
2. 启动 `python app.py`。
3. 进入设置页后触发论文更新与翻译。
4. 在首页查看论文列表，按标签、日期、关键词筛选。
5. 点击生成总结，获取围绕关注问题的每日论文汇总。
6. 点击单篇论文分析，系统会下载 PDF、提取文本并生成阅读结论。

## 数据输出

项目会按日期和领域组织论文数据，典型目录如下：

```text
file/
└── 2026/4/8/cs.RO/
    ├── papers.json
    ├── papers_zh.json
    └── summary_response.json
```

另外，单篇论文分析结果默认保存在：

```text
file/analysis/
├── <paper-hash>.json
└── pdf/<paper-hash>.pdf
```

