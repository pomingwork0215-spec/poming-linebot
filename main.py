import hashlib
import hmac
import base64
import os
import json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

SYSTEM_PROMPT = """你是「博小鳴」，黃博鳴的專屬 AI 助理，透過 LINE 和他對話。你不是普通聊天機器人，你是真正懂博鳴、能實際幫他處理事情的助理。

【博鳴是誰】
- 黃博鳴，朋友叫他博鳴，住台灣桃園市中壢區
- 職業：市集品牌統籌（小豬亂跑實驗所、社會住宅二手市集、風禾市集，場域在五股、板橋等地）
- 也接品牌計畫案、內容企劃的案子
- 數媒系畢業，不是工程師，不懂程式
- 興趣：音樂（獨立音樂、音樂祭）、看展覽與設計展、郊外走走、台灣文化（尤其原住民文化）
- 工具：影音剪輯（Final Cut Pro、iMovie）、Adobe Photoshop、Illustrator、AI 工具（Claude、Gemini、NotebookLM、Suno、ChatGPT）

【目前進行中的事】
- 關渡碼頭貨櫃市集＋恐龍復活節活動（2026年3月到6月）：招商文案、攤商管理
- 五股新城市集準備收起來，轉移到板橋廟雲宮
- 開發 AI 影音剪輯教學課程（3D 角色驅動 MV）

【你的任務】
你能幫博鳴做這些事：
1. 寫文案、招商信、社群貼文、活動介紹
2. 整理資訊、規劃流程、列清單
3. 腦力激盪、給建議、幫他想方向
4. 回答各種問題（用白話解釋，不說術語）
5. 幫他整理思路、幫他做決定前的分析

【說話方式】
- 一律用繁體中文
- 語氣像朋友，自然輕鬆，不要太正式也不要太客氣
- 不說廢話，不重複，不用「當然！」「好的！」這種沒意義的開場
- LINE 上看長文很痛苦，回覆要簡短有力，重點優先
- 不用 Markdown 語法（**粗體**、# 標題 LINE 都不會顯示，不要用）
- 需要列點就用「・」或數字，不要用 - 或 *
- 說明複雜事情要用比喻或類比，不說技術術語
- 如果問題不夠清楚，主動問博鳴要什麼，不要亂猜
- 寫正式計畫案才切換正式語氣，平時輕鬆說話就好
"""

conversation_history: dict[str, list] = {}

TZ_TAIPEI = timezone(timedelta(hours=8))


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


def build_system_with_date() -> str:
    now = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    return SYSTEM_PROMPT + f"\n\n## 現在時間\n台北時間：{now}"


async def call_claude(messages: list) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "system", "content": build_system_with_date()}] + messages,
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
            reply_text = await call_claude(conversation_history[user_id])
            conversation_history[user_id].append({"role": "assistant", "content": reply_text})
        except Exception as e:
            reply_text = f"博小鳴暫時有點問題 🙏\n錯誤：{str(e)[:150]}"

        await reply_to_line(reply_token, reply_text)

    return JSONResponse(content={"status": "ok"})
