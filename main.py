import hashlib
import hmac
import base64
import os
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

SYSTEM_PROMPT = """你是「博小鳴」，黃博鳴的 AI 助理分身，透過 LINE 和博鳴對話。

## 關於博鳴
- 姓名：黃博鳴，朋友都叫他博鳴
- 職業：市集品牌統籌與管理員（小豬亂跑實驗所、社會住宅二手市集、風禾市集 — 五股、板橋等地）
- 自由接案：品牌計畫案、內容企劃
- 居住地：台灣桃園市中壢區
- 興趣：影音剪輯（Final Cut Pro）、茶文化（南投高山茶）、AI 工具應用、日系美學服飾

## 你的行為準則
- 一律使用繁體中文
- 語氣自然輕鬆，像朋友對話，不要太正式
- 簡潔有力，不說廢話
- 可以幫博鳴查資料、規劃行程、寫文案、腦力激盪、回答各種問題
- 若需要更多資訊，主動詢問
- 回覆長度適中，不要太長（LINE 上看長文很痛苦）
"""

conversation_history: dict[str, list] = {}


def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return True
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(
        base64.b64encode(hash_value).decode("utf-8"),
        signature,
    )


async def call_groq(messages: list) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                "max_tokens": 1024,
                "temperature": 0.7,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def reply_to_line(reply_token: str, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=30,
        )


@app.get("/")
async def root():
    return {"status": "博小鳴 LINE Bot 運行中 ✅"}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"]["type"] != "text":
            continue

        user_message = event["message"]["text"]
        reply_token = event["replyToken"]
        user_id = event["source"].get("userId", "unknown")

        if user_id not in conversation_history:
            conversation_history[user_id] = []

        conversation_history[user_id].append({"role": "user", "content": user_message})

        if len(conversation_history[user_id]) > 20:
            conversation_history[user_id] = conversation_history[user_id][-20:]

        try:
            reply_text = await call_groq(conversation_history[user_id])
            conversation_history[user_id].append({"role": "assistant", "content": reply_text})
        except Exception as e:
            reply_text = f"博小鳴暫時有點問題，請稍後再試 🙏（{str(e)[:80]}）"

        await reply_to_line(reply_token, reply_text)

    return JSONResponse(content={"status": "ok"})
