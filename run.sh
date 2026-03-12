#!/bin/bash
# 启动论文预览网页服务
# 可通过 http://<本机IP>:5000 访问
cd "$(dirname "$0")"
python3 app.py
