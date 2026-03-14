import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import requests
from openai import OpenAI
from config import get

class model_api:
    def __init__(self):
        pass

    def get_balance(self):
        print("余额查询失败")
    
    def inference(self):
        print("模型未选取")
        pass

class deepseek_api(model_api):
    def __init__(self):
        self.model = get("model.model", "deepseek-chat")

    def get_balance(self, api_key=None):
        api_key = api_key or get("model.api_key") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            print("请配置 model.api_key 或设置 DEEPSEEK_API_KEY")
            return None
        url = "https://api.deepseek.com/user/balance"
        payload = {}
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}

        response = json.loads(requests.request("GET", url, headers=headers, data=payload).text)
        if response.get("is_available"):
            return response["balance_infos"][0]["total_balance"]
        print("获取余额失败")
        return None

    def inference(self, messages, api_key=None, pdf_path = None):
        api_key = api_key or get("model.api_key") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            print("请配置 model.api_key 或设置 DEEPSEEK_API_KEY")
            return None
        try:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(model=self.model, messages=messages, stream=False)
            return response.choices[0].message.content
        except Exception as e:
            print(f"API 调用失败: {e}")
            return None

def load_model():
    model_name = get("model.model")
    if model_name in ["deepseek-chat", "deepseek-reasoner"]:
        return deepseek_api()
    else:
        print(f"未找到模型: {model_name}")
        return None

if __name__ == "__main__":
    api = deepseek_api()
    balance = api.get_balance()
    print(f"余额: {balance}")
    # messages = [{"role": "system", "content": "You are a helpful assistant"}, {"role": "user", "content": "Hello"}]
    # reply = api.inference(messages)
    # print(reply)