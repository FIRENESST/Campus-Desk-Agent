import json
import os
from openai import OpenAI
from pathlib import Path

history_file = Path(__file__).with_name("history.json")
MAX_TURNS = 10

client = OpenAI(base_url=os.getenv("CAMPUS_CHAT_BASE_URL", "http://127.0.0.1:8000/v1"),
                api_key=os.environ["CAMPUS_CHAT_API_KEY"])

if history_file.exists():
    messages = json.loads(
        history_file.read_text(encoding="utf-8")
    )
else:
    messages = [
        {
            "role": "system",
            "content": "你是一个乐于助人的助手。",
        }
    ]
while True:
    user = input()
    messages.append({"role":"user","content":user})

    stream = client.chat.completions.create(
        model = "deepseek-flash",
        messages = messages,
        stream = True
    )
    print("AI: ", end="", flush=True)
    reply = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
            reply += delta
    print()
    messages.append({"role": "assistant", "content": reply})

    system_massage = messages[0]
    history = messages[1:]
    messages = [system_massage,*history[-(MAX_TURNS*2):],]
    history_file.write_text(json.dumps(messages, ensure_ascii=False, indent=2),encoding="utf-8",)
