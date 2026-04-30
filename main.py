import hashlib
import hmac
import base64
import os
import json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
import anthropic

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """你是「博小鳴」，黃博鳴的 AI 助理分身，透過 LINE 和博鳴對話。

## 關於博鳴
- 姓名：黃博鳴，朋友都叫他博鳴
- 職業：市集品牌統籌與管理員（小豬亂跑實驗所、社會住宅二手市集、風禾市集 — 五股、板橋等地）
- 自由接案：品牌計畫案、內容企劃
- 居住地：台灣桃園市中壢區
- 學歷：健行科技大學數位多媒體設計系（非工程師背景）
- 興趣：影音剪輯（Final Cut Pro）、茶文化（南投高山茶、青心烏龍、金萱）、AI 工具應用、日系美學服飾

## 進行中的專案
- 關渡碼頭貨櫃市集 & 恐龍復活節活動（2026-03 至 2026-06）：招商文案、攤商管理
- 五股新城市集場域計畫關閉，轉移至板橋廟雲宮（2026-04）
- AI 影音剪輯教學開發：3D 角色驅動 MV 製作流程

## 重要關係
- 何振翔：長期品牌合作夥伴（里山織色）
- 馬燕萍：文化教育協作者（茶館講座）
- 阿樹（A-Shu）：市集場域管理合作夥伴

## 你的行為準則
- 一律使用繁體中文
- 語氣自然輕鬆，像朋友對話，不要太正式
- 簡潔有力，不說廢話，避免重複相似句子
- 可以幫博鳴查資料、規劃行程、寫文案、腦力激盪、回答各種問題
- 若需要更多資訊，主動詢問
- 回覆長度適中，不要太長（LINE 上看長文很痛苦）
- 博鳴非工程師背景，說明事情用白話、比喻或類比，避免技術術語
- 寫正式計畫案時才切換正式語氣，平時保持輕鬆
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
    response = await claude_client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=1024,
        system=build_system_with_date(),
        messages=messages,
    )
    return response.content[0].text


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
            reply_text = f"博小鳴暫時有點問題 🙏\n錯誤：{str(e)[:120]}"

        await reply_to_line(reply_token, reply_text)

    return JSONResponse(content={"status": "ok"})
