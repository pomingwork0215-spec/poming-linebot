import hashlib
import hmac
import base64
import os
import json
import re
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

STALL_MAP_URL = "https://raw.githubusercontent.com/pomingwork0215-spec/poming-linebot/main/assets/stall-map.jpg"
LINE_USER_ID = "Uf22e9c4891b5e67dd8fa6f80ccb56696"

# 記憶最近一次的攤位資料（伺服器重啟後會清空，但通常可撐過一夜）
_last_stall_text: str | None = None       # 配置文案（回給攤商看的）
_last_stall_vendors: list | None = None   # 攤商名稱清單（用於生成社群公告）

SYSTEM_PROMPT = """你是「博小鳴」，黃博鳴的專屬 AI 助理，透過 LINE 和他對話。你不是普通聊天機器人，你是真正懂博鳴、能實際幫他處理事情的助理。

【博鳴是誰】
- 黃博鳴，朋友叫他博鳴，住台灣桃園市中壢區
- 職業：市集品牌統籌，經營小豬亂跑實驗所（附設舊物再造所二手市集）、舊物慢旅二手市集（社會住宅）、風禾市集，場域在五股、板橋等地
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

WEEKDAY_ZH = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "日"}


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


def is_stall_arrangement(message: str) -> bool:
    """判斷是否為攤位安排格式（含 1號: 或 1號：）"""
    return bool(re.search(r'[1-4]號\s*[：:]', message))


def parse_stall_arrangement(message: str) -> dict:
    """解析攤位安排，回傳 {1: '攤商名｜描述', ...}"""
    stalls = {}
    pattern = r'([1-4])號\s*[：:]\s*([^\n]+)'
    for match in re.finditer(pattern, message):
        pos = int(match.group(1))
        vendor = match.group(2).strip()
        if vendor and vendor not in ['空', '無', '']:
            stalls[pos] = vendor
    return stalls


def extract_vendor_names(stalls: dict) -> list:
    """從攤位字典提取攤商名稱（不含描述）"""
    names = []
    for pos in sorted(stalls.keys()):
        vendor = stalls[pos]
        name = re.split(r'[｜|]', vendor)[0].strip()
        names.append(name)
    return names


def generate_community_text(vendors: list) -> str:
    """生成社群公告文案（14:00 格式）"""
    today = datetime.now(TZ_TAIPEI)
    date_str = f"{today.month}月{today.day} 日"
    weekday = WEEKDAY_ZH.get(today.strftime("%u"), "")
    count = len(vendors)

    lines = [
        f"日期 {date_str}（{weekday}）",
        f"活動 【今日 {count} 攤美食】",
    ]
    for i, name in enumerate(vendors, 1):
        lines.append(f"{i}. {name}")
    lines.append("🍢🍖🧅🥩🌽🍗🥟🍜🍱")
    return "\n".join(lines)


def generate_stall_text(stalls: dict) -> str:
    """生成板橋妙雲宮攤位配置文案"""
    tomorrow = datetime.now(TZ_TAIPEI) + timedelta(days=1)
    date_str = f"{tomorrow.month}/{tomorrow.day}"
    weekday = WEEKDAY_ZH.get(tomorrow.strftime("%u"), "")

    lines = [
        "《板橋妙雲宮市集區》",
        f"{date_str}（{weekday}） 攤位配置更新如下",
    ]

    for pos in sorted(stalls.keys()):
        vendor = stalls[pos]
        # 若有描述（含 | 或 ｜），格式：@攤商｜描述；否則：@攤商
        if '｜' in vendor or '|' in vendor:
            parts = re.split(r'[｜|]', vendor, 1)
            lines.append(f"{pos}號：@{parts[0].strip()}｜{parts[1].strip()}")
        else:
            lines.append(f"{pos}號：@{vendor}")

    lines += [
        "以下提醒：",
        "① 請落地攤卸貨完務必將車輛移出場域",
        "② 請2號攤位請不要正對廟門",
        "！！~謝謝老闆的配合～！！",
    ]
    return "\n".join(lines)


async def push_line_message(text: str):
    """主動推送文字訊息給博鳴"""
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "to": LINE_USER_ID,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=30,
        )


async def reply_stall_arrangement(reply_token: str, stall_text: str, vendors: list):
    """回覆攤位圖＋配置文案，並儲存供 13:30 使用"""
    global _last_stall_text, _last_stall_vendors
    _last_stall_text = stall_text
    _last_stall_vendors = vendors
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "replyToken": reply_token,
                "messages": [
                    {
                        "type": "image",
                        "originalContentUrl": STALL_MAP_URL,
                        "previewImageUrl": STALL_MAP_URL,
                    },
                    {
                        "type": "text",
                        "text": stall_text,
                    },
                ],
            },
            timeout=30,
        )


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


@app.get("/send-today-stall")
async def send_today_stall():
    """13:30 排程呼叫此端點，傳送今日社群公告文案給博鳴確認"""
    global _last_stall_vendors
    if _last_stall_vendors:
        community_text = generate_community_text(_last_stall_vendors)
        confirm_text = f"📋 今日社群公告確認：\n\n{community_text}\n\n確認沒問題的話，複製發到社群吧！"
        await push_line_message(confirm_text)
        return {"status": "ok", "mode": "stored"}
    else:
        remind_text = "📢 13:30 囉！\n\n昨晚的攤位資料找不到，請把今天的攤位安排傳給博小鳴：\n\n1號：攤商名\n2號：攤商名\n3號：攤商名\n4號：攤商名"
        await push_line_message(remind_text)
        return {"status": "ok", "mode": "fallback"}


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

        # 攤位安排模式：偵測到 1號: 格式，自動回傳攤位圖＋文案
        if is_stall_arrangement(user_message):
            stalls = parse_stall_arrangement(user_message)
            if stalls:
                vendors = extract_vendor_names(stalls)
                stall_text = generate_stall_text(stalls)
                await reply_stall_arrangement(reply_token, stall_text, vendors)
                return JSONResponse(content={"status": "ok"})

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
