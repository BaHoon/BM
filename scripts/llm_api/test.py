import os
# 强制要求 Python 网络库遇到同济的域名时不走任何代理
os.environ["NO_PROXY"] = "llmapi.tongji.edu.cn,localhost,127.0.0.1"

from openai import OpenAI

client = OpenAI(
    api_key = "your_api_key",   # 记得替换为你的API密钥
    base_url = "https://llmapi.tongji.edu.cn/v1"
)
chat_completion = client.chat.completions.create(
    model="DeepSeek-R1",
    messages=[
        {
            "role": "user",
            "content": "地球的半径是多少?",
        }
    ]
)

print(chat_completion.choices[0].message.content)