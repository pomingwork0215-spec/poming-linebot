import hashlib
import hmac
import base64
import os
import json
import re
from io import BytesIO
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response
import httpx
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

STALL_MAP_URL = "https://raw.githubusercontent.com/pomingwork0215-spec/poming-linebot/main/assets/stall-map.jpg"
LINE_USER_ID = "Uf22e9c4891b5e67dd8fa6f80ccb56696"
LINE_GROUP_ID = "C84e007a900a9aeb39e9baaf464af008d"  # 風禾社群小幫手
STALL_VENDORS_FILE = "/tmp/stall_vendors.json"

# ── 大嵙崁志工報班系統：廟宇親子館「明天到班志工」自動推播 ──
TEMPLE_LINE_GROUP_ID = os.environ.get("TEMPLE_LINE_GROUP_ID", "")
TEMPLE_FIREBASE_CREDENTIALS_B64 = os.environ.get("TEMPLE_FIREBASE_CREDENTIALS_B64", "")
TEMPLE_VENUE_ID = "SzK7RjDIDxTDzrdux8ZD"  # 廟宇親子館
TEMPLE_VENUE_NAME = "廟宇親子館"
RENDER_BASE_URL = "https://poming-linebot-2.onrender.com"
_temple_duty_image_bytes: bytes | None = None
_temple_duty_image_version: int = 0
_last_event_source: dict | None = None  # 除錯用：記住最近一次收到訊息的來源（抓新群組 ID 用）

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


def generate_community_text(vendors: list, date_offset: int = 0) -> str:
    """生成社群公告文案"""
    today = datetime.now(TZ_TAIPEI) + timedelta(days=date_offset)
    date_str = f"{today.month}月{today.day}日"
    weekday = WEEKDAY_ZH.get(today.strftime("%u"), "")
    count = len(vendors)

    lines = [
        f"📅 日期　{date_str}（{weekday}）",
        f"🍱 今日　{count} 攤美食",
        "🔥🍢🍖🥩🌽🍗🥟🍜🔥",
        "",
    ]
    for name in vendors:
        lines.append(f"✅ {name}")
    return "\n".join(lines)


def generate_stall_text(stalls: dict, date_offset: int = 1) -> str:
    """生成板橋妙雲宮攤位配置文案"""
    target = datetime.now(TZ_TAIPEI) + timedelta(days=date_offset)
    date_str = f"{target.month}/{target.day}"
    weekday = WEEKDAY_ZH.get(target.strftime("%u"), "")

    if date_offset == 0:
        # 白天回覆（今日攤位）：簡潔格式
        count = len(stalls)
        lines = [
            "《板橋妙雲宮市集區》",
            f"{date_str}（{weekday}）攤位配置",
            f"今日共 {count} 攤",
            "",
        ]
        for pos in sorted(stalls.keys()):
            vendor = stalls[pos]
            if '｜' in vendor or '|' in vendor:
                parts = re.split(r'[｜|]', vendor, 1)
                lines.append(f"{pos}號：@{parts[0].strip()}｜{parts[1].strip()}")
            else:
                lines.append(f"{pos}號：@{vendor}")
    else:
        # 晚上回覆（確認明日攤位）：原格式含提醒
        lines = [
            "《板橋妙雲宮市集區》",
            f"{date_str}（{weekday}） 攤位配置更新如下",
        ]
        for pos in sorted(stalls.keys()):
            vendor = stalls[pos]
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


async def push_line_message(text: str, target_id: str = None):
    """主動推送文字訊息，預設傳給博鳴個人，可指定 target_id 傳到群組"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "to": target_id or LINE_USER_ID,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise Exception(f"LINE API error {resp.status_code}: {resp.text}")


_temple_db = None


def get_temple_db():
    """延遲初始化 temple-signup-system 專案的 Firestore admin client（跟 LINE Bot 是不同的 Firebase 專案）"""
    global _temple_db
    if _temple_db is not None:
        return _temple_db
    if not TEMPLE_FIREBASE_CREDENTIALS_B64:
        raise RuntimeError("尚未設定 TEMPLE_FIREBASE_CREDENTIALS_B64 環境變數")
    import firebase_admin
    from firebase_admin import credentials, firestore

    cred_json = json.loads(base64.b64decode(TEMPLE_FIREBASE_CREDENTIALS_B64))
    cred = credentials.Certificate(cred_json)
    app_instance = firebase_admin.initialize_app(cred, name="temple")
    _temple_db = firestore.client(app_instance)
    return _temple_db


def shift_capacity_for_date(shift: dict, date_str: str, is_holiday: bool, overrides: list) -> int:
    override = next((o for o in overrides if o.get("date") == date_str and o.get("shiftId") == shift.get("id")), None)
    if override is not None:
        try:
            return int(override.get("capacity") or 0)
        except (TypeError, ValueError):
            return 0
    if is_holiday and shift.get("weekendCapacity") not in (None, ""):
        return int(shift["weekendCapacity"])
    try:
        return int(shift.get("capacity") or 0)
    except (TypeError, ValueError):
        return 0


def get_temple_duty_data(date_str: str) -> dict:
    """查詢廟宇親子館某天的排班資料，整理成畫卡片要用的結構"""
    db = get_temple_db()
    venue_ref = db.collection("venues").document(TEMPLE_VENUE_ID)

    settings_doc = venue_ref.collection("settings").document("main").get()
    settings = settings_doc.to_dict() if settings_doc.exists else {}
    shifts = settings.get("shifts") or [
        {"id": "am", "name": "上午班", "startTime": "09:00", "endTime": "13:00", "capacity": 3, "weekendCapacity": 3},
        {"id": "pm", "name": "下午班", "startTime": "13:00", "endTime": "17:00", "capacity": 3, "weekendCapacity": 3},
    ]
    holidays = settings.get("holidays") or []
    closed_dates = settings.get("closedDates") or []
    capacity_overrides = settings.get("capacityOverrides") or []

    target = datetime.strptime(date_str, "%Y-%m-%d")
    weekday_iso = target.isoweekday()  # 1=一 ... 7=日
    is_weekend = weekday_iso in (6, 7)
    is_holiday = is_weekend or date_str in holidays
    is_closed = date_str in closed_dates

    schedules = [
        d.to_dict() for d in venue_ref.collection("schedules").where("date", "==", date_str).stream()
    ]
    duty_entries = [
        d.to_dict() for d in venue_ref.collection("dutyRoster").where("date", "==", date_str).stream()
    ]

    shifts_sorted = sorted(shifts, key=lambda s: s.get("startTime", ""))
    shift_rows = []
    has_shortage = False
    for shift in shifts_sorted:
        entries = [s for s in schedules if s.get("shiftId") == shift.get("id")]
        names = [e.get("volunteerName", "") for e in entries if e.get("volunteerName")]
        cap = shift_capacity_for_date(shift, date_str, is_holiday, capacity_overrides)
        if cap > 0 and len(entries) < cap:
            has_shortage = True
        shift_rows.append((shift.get("name", ""), names))

    status = "closed" if is_closed else ("short" if has_shortage else "full")
    duty_names = [e.get("volunteerName", "") for e in duty_entries if e.get("volunteerName")]

    return {
        "date_label": f"{target.month}/{target.day}",
        "weekday_label": f"週{WEEKDAY_ZH.get(str(weekday_iso), '')}",
        "is_holiday": is_holiday,
        "status": status,
        "shift_rows": shift_rows,
        "duty_names": duty_names,
    }


_TEMPLE_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_TEMPLE_C = {
    "black": "#1F4A57", "cream": "#F3F7F8", "cream_dark": "#DCE6E9",
    "gray": "#6D7C82", "gray_light": "#A9B7BC", "white": "#FFFFFF",
    "green": "#4CAF50", "red": "#C94B4B", "yellow_light": "#FBF0C4",
    "blue": "#4A90D2", "duty_bg": "#E8F0FA",
}


def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def render_temple_duty_card(data: dict) -> bytes:
    """把 get_temple_duty_data() 的結果畫成一張卡片圖片（PNG bytes），樣式比照報班頁「排班總覽」卡片"""
    c = _TEMPLE_C
    regular = os.path.join(_TEMPLE_FONT_DIR, "NotoSansTC-Regular.ttf")
    bold = os.path.join(_TEMPLE_FONT_DIR, "NotoSansTC-Bold.ttf")

    duty_names = data["duty_names"]
    shift_rows = data["shift_rows"]

    R, BAR_W, PAD_X, PAD_Y, DATE_COL_W = 32, 12, 44, 40, 210
    DUTY_COL_W = 190 if duty_names else 0
    W = 920 + DUTY_COL_W

    font_venue = ImageFont.truetype(bold, 26)
    font_date = ImageFont.truetype(bold, 68)
    font_weekday = ImageFont.truetype(regular, 28)
    font_badge = ImageFont.truetype(bold, 20)
    font_shift = ImageFont.truetype(bold, 30)
    font_chip = ImageFont.truetype(bold, 26)
    font_hint = ImageFont.truetype(regular, 24)
    font_duty_label = ImageFont.truetype(bold, 20)

    row_h = 78
    content_h = PAD_Y * 2 + 44 + len(shift_rows) * row_h
    duty_h = PAD_Y * 2 + 40 + len(duty_names) * 54
    H = max(content_h, duty_h)

    img = Image.new("RGB", (W, H), c["white"])
    draw = ImageDraw.Draw(img)

    status_color = {"full": c["green"], "short": c["red"], "closed": c["cream_dark"]}[data["status"]]
    draw.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=status_color)
    draw.rounded_rectangle((BAR_W, 0, W - 1, H - 1), radius=R, fill=c["white"])

    draw.text((PAD_X + DATE_COL_W, PAD_Y - 8), TEMPLE_VENUE_NAME, font=font_venue, fill=c["gray"])

    date_cx = PAD_X + DATE_COL_W // 2
    date_top = PAD_Y + 30
    w, h = _text_size(draw, data["date_label"], font_date)
    draw.text((date_cx - w / 2, date_top), data["date_label"], font=font_date, fill=c["black"])

    w, h = _text_size(draw, data["weekday_label"], font_weekday)
    weekday_top = date_top + 78
    draw.text((date_cx - w / 2, weekday_top), data["weekday_label"], font=font_weekday, fill=c["gray"])

    if data["is_holiday"]:
        badge_text = "假日"
        w, h = _text_size(draw, badge_text, font_badge)
        bw, bh = w + 24, h + 14
        bx, by = date_cx - bw / 2, weekday_top + 44
        draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh / 2, fill=c["yellow_light"])
        draw.text((bx + 12, by + 6), badge_text, font=font_badge, fill="#8A6D1A")

    content_x = PAD_X + DATE_COL_W + 20
    content_right = W - PAD_X - DUTY_COL_W
    y = PAD_Y + 44
    for shift_name, names in shift_rows:
        row_center = y + row_h / 2
        w, h = _text_size(draw, shift_name, font_shift)
        draw.text((content_x, row_center - h / 2 - 4), shift_name, font=font_shift, fill=c["black"])

        chip_x = content_x + 150
        if not names:
            draw.text((chip_x, row_center - 12), "尚無人排班", font=font_hint, fill=c["gray_light"])
        else:
            cx, cy_top = chip_x, row_center - 24
            for name in names:
                w, h = _text_size(draw, name, font_chip)
                cw, ch = w + 34, 48
                if cx + cw > content_right:
                    cx, cy_top = chip_x, cy_top + ch + 10
                draw.rounded_rectangle((cx, cy_top, cx + cw, cy_top + ch), radius=ch / 2,
                                        fill=c["cream"], outline=c["cream_dark"], width=2)
                draw.text((cx + 17, cy_top + (ch - h) / 2 - 4), name, font=font_chip, fill=c["black"])
                cx += cw + 10
        y += row_h

    if duty_names:
        divider_x = W - PAD_X - DUTY_COL_W + 10
        draw.line((divider_x, PAD_Y, divider_x, H - PAD_Y), fill=c["cream_dark"], width=3)

        duty_col_x = divider_x + 30
        label_text = "館務員"
        w, h = _text_size(draw, label_text, font_duty_label)
        label_top = H / 2 - (h + 12 + len(duty_names) * 54) / 2
        draw.text((duty_col_x, label_top), label_text, font=font_duty_label, fill=c["gray"])

        chip_top = label_top + h + 18
        for name in duty_names:
            w, h = _text_size(draw, name, font_chip)
            cw, ch = w + 34, 48
            draw.rounded_rectangle((duty_col_x, chip_top, duty_col_x + cw, chip_top + ch), radius=ch / 2,
                                    fill=c["duty_bg"], outline=c["blue"], width=2)
            draw.text((duty_col_x + 17, chip_top + (ch - h) / 2 - 4), name, font=font_chip, fill=c["blue"])
            chip_top += ch + 10

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def reply_stall_arrangement(reply_token: str, stall_text: str, vendors: list, with_image: bool = False):
    """回覆攤位配置文案，並儲存攤商清單供 13:30 自動推播"""
    global _last_stall_text, _last_stall_vendors
    _last_stall_text = stall_text
    _last_stall_vendors = vendors
    with open(STALL_VENDORS_FILE, "w", encoding="utf-8") as f:
        json.dump(vendors, f, ensure_ascii=False)
    messages = []
    if with_image:
        messages.append({
            "type": "image",
            "originalContentUrl": STALL_MAP_URL,
            "previewImageUrl": STALL_MAP_URL,
        })
    messages.append({"type": "text", "text": stall_text})
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "replyToken": reply_token,
                "messages": messages,
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
    """13:30 排程：有前一晚攤商清單則直接推社群公告，否則退回詢問模式"""
    try:
        with open(STALL_VENDORS_FILE, encoding="utf-8") as f:
            vendors = json.load(f)
        if vendors:
            community_text = generate_community_text(vendors)
            await push_line_message(community_text, target_id=LINE_GROUP_ID)
            return {"status": "ok"}
    except Exception:
        pass

    taipei_tz = timezone(timedelta(hours=8))
    today = datetime.now(taipei_tz)
    weekday_names = ['一', '二', '三', '四', '五', '六', '日']
    weekday = weekday_names[today.weekday()]
    date_str = f"{today.month}/{today.day}"
    msg = (
        f"📢 板橋妙雲宮今天（{date_str} {weekday}）攤位安排確認！\n\n"
        "請依格式回覆（空位填「空」或留空）：\n"
        "1號：\n2號：\n3號：\n4號："
    )
    await push_line_message(msg, target_id=LINE_GROUP_ID)
    return {"status": "ok", "mode": "fallback"}


@app.get("/send-arrived")
async def send_arrived():
    """16:30 排程呼叫，自動生成攤商到齊通知，傳到風禾社群小幫手群組"""
    prompt = "注意：絕對禁止出現「市集」這個詞。生成一則攤商已到齊、邀請大家來妙雲宮的 LINE 社群通知。開頭必須是 @All（A 大寫）、一到兩句話、語氣誇張有趣像在呼朋引伴吃好料、用「來逛逛」「快來吃」「快來」之類的口語表達、加 1~2 個 emoji、每次都要不一樣。只輸出文案本身，不要任何說明。"
    text = await call_claude([{"role": "user", "content": prompt}])
    await push_line_message(text, target_id=LINE_GROUP_ID)
    return {"status": "ok"}


@app.get("/send-come-now")
async def send_come_now():
    """18:30 排程呼叫，自動生成晚餐吆喝文案，傳到風禾社群小幫手群組"""
    prompt = "生成一則「晚餐時間快來板橋妙雲宮」的 LINE 社群訊息。規則：不用 @All 開頭、強調晚餐時間或夜晚氛圍、口語化像跟朋友說話、加 1~2 個 emoji、每次都要不一樣。只輸出文案，不要其他說明。"
    text = await call_claude([{"role": "user", "content": prompt}])
    await push_line_message(text, target_id=LINE_GROUP_ID)
    return {"status": "ok"}


@app.get("/debug-push")
async def debug_push():
    """除錯用：直接呼叫 LINE push API 並回傳完整回應"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={
                "to": LINE_GROUP_ID,
                "messages": [{"type": "text", "text": "除錯測試訊息"}],
            },
            timeout=30,
        )
        return {
            "status_code": resp.status_code,
            "body": resp.text,
            "group_id": LINE_GROUP_ID,
            "token_prefix": LINE_CHANNEL_ACCESS_TOKEN[:20] + "..." if LINE_CHANNEL_ACCESS_TOKEN else "空白",
        }


@app.get("/send-confirm-tomorrow")
async def send_confirm_tomorrow():
    """20:30 排程呼叫，傳送明日攤位詢問到風禾社群小幫手群組"""
    taipei_tz = timezone(timedelta(hours=8))
    tomorrow = datetime.now(taipei_tz) + timedelta(days=1)
    weekday_names = ['一', '二', '三', '四', '五', '六', '日']
    weekday = weekday_names[tomorrow.weekday()]
    date_str = f"{tomorrow.month}/{tomorrow.day}"
    msg = (
        f"板橋妙雲宮明天（{date_str} {weekday}）攤位怎麼安排？\n\n"
        "位置說明：\n"
        "・1號｜適合大車（靠重慶路側）\n"
        "・2號｜小攤（廟門前方）\n"
        "・3號｜適合大車（靠公園路側）\n"
        "・4號｜小攤（正門右側）\n\n"
        "請依格式填寫（空位填「空」或留空）：\n"
        "1號：\n2號：\n3號：\n4號："
    )
    try:
        await push_line_message(msg, target_id=LINE_GROUP_ID)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/debug-last-source")
async def debug_last_source():
    """除錯用：查詢最近一次收到 LINE 訊息的來源（新群組要抓 groupId 時，先在群組裡發一則訊息，再打這支）"""
    return _last_event_source or {"status": "尚未收到任何訊息"}


@app.get("/temple-duty-image.png")
async def temple_duty_image():
    """給 LINE 抓圖用：回傳最近一次產生的「明天到班志工」卡片圖"""
    if not _temple_duty_image_bytes:
        raise HTTPException(status_code=404, detail="尚未產生過圖片")
    return Response(content=_temple_duty_image_bytes, media_type="image/png")


@app.get("/send-temple-duty-tomorrow")
async def send_temple_duty_tomorrow():
    """每天 18:00 排程呼叫：產生明天廟宇親子館的到班志工卡片圖，推送到指定 LINE 群組"""
    global _temple_duty_image_bytes, _temple_duty_image_version
    if not TEMPLE_LINE_GROUP_ID:
        return {"status": "error", "detail": "尚未設定 TEMPLE_LINE_GROUP_ID 環境變數"}
    try:
        tomorrow = datetime.now(TZ_TAIPEI) + timedelta(days=1)
        date_str = tomorrow.strftime("%Y-%m-%d")
        data = get_temple_duty_data(date_str)
        _temple_duty_image_bytes = render_temple_duty_card(data)
        _temple_duty_image_version += 1

        image_url = f"{RENDER_BASE_URL}/temple-duty-image.png?v={_temple_duty_image_version}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                },
                json={
                    "to": TEMPLE_LINE_GROUP_ID,
                    "messages": [{
                        "type": "image",
                        "originalContentUrl": image_url,
                        "previewImageUrl": image_url,
                    }],
                },
                timeout=30,
            )
        if resp.status_code != 200:
            return {"status": "error", "detail": f"LINE API {resp.status_code}: {resp.text}"}
        return {"status": "ok", "date": date_str}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/send-morning-report")
async def send_morning_report():
    """中午12:00 排程呼叫：生成每日早報並傳給博鳴"""
    now = datetime.now(TZ_TAIPEI)
    weekday = WEEKDAY_ZH.get(now.strftime("%u"), "")
    date_str = f"{now.year}-{now.month:02d}-{now.day:02d}"

    # 取得天氣
    weather_lines = []
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://wttr.in/Zhongli+Taoyuan,Taiwan?format=j1", timeout=10
            )
            wdata = r.json()
            cc = wdata["current_condition"][0]
            td = wdata["weather"][0]
            raw_desc = td["hourly"][0]["weatherDesc"][0]["value"]
            desc_map = {
                "Sunny": "晴天", "Clear": "晴天",
                "Partly cloudy": "多雲時晴", "Partly Cloudy": "多雲時晴",
                "Cloudy": "陰天", "Overcast": "陰天",
                "Mist": "霧", "Fog": "霧",
                "Light drizzle": "毛毛雨",
                "Light rain": "短暫陣雨", "Light rain shower": "短暫陣雨",
                "Patchy rain possible": "局部有雨",
                "Moderate rain": "中雨", "Heavy rain": "大雨",
                "Thundery outbreaks possible": "雷陣雨",
            }
            desc_zh = desc_map.get(raw_desc, raw_desc)
            max_t = td["maxtempC"]
            min_t = td["mintempC"]
            feels = cc["FeelsLikeC"]
            wind = cc["windspeedKmph"]
            rain = sum(float(h["precipMM"]) for h in td["hourly"])
            weather_lines.append(f"{desc_zh}，最高 {max_t}°C / 最低 {min_t}°C")
            weather_lines.append(f"體感溫度 {feels}°C，風速 {wind} km/h")
            if rain > 0:
                weather_lines.append(f"預計降雨 {rain:.1f}mm，記得帶傘！")
    except Exception:
        weather_lines.append("天氣資料暫時無法取得")

    weather_text = "\n".join(weather_lines)
    prompt = (
        f"今天是 {date_str}（{weekday}），台北中壢的天氣：{weather_text}。\n\n"
        "請幫博鳴生成今天的早報。格式如下，只輸出內容本身，不要任何說明：\n\n"
        f"☀️ 博鳴早報｜{date_str}（{weekday}）\n\n"
        f"🌤【今日天氣】\n{weather_text}\n\n"
        "✅【今日建議優先處理】\n（根據今天是星期幾給 1-2 個具體建議）\n\n"
        "🎵【今日獨立音樂推薦】\n（台灣或亞洲獨立音樂一首，格式：歌手 - 歌名，一句話說明）\n\n"
        "💪【今日一句】\n（輕鬆幽默有力量，以「博鳴！」結尾）"
    )
    text = await call_claude([{"role": "user", "content": prompt}])
    await push_line_message(text, target_id=LINE_USER_ID)
    return {"status": "ok"}


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
        print(f"[SOURCE] {event['source']}", flush=True)
        global _last_event_source
        _last_event_source = event["source"]

        # 攤位安排模式：偵測到 1號: 格式，自動回傳攤位圖＋文案
        if is_stall_arrangement(user_message):
            stalls = parse_stall_arrangement(user_message)
            if stalls:
                vendors = extract_vendor_names(stalls)
                # 20:00 以前視為今天，20:00 以後視為明天（配合 20:30 詢問明日攤位的排程）
                hour = datetime.now(TZ_TAIPEI).hour
                date_offset = 0 if hour < 20 else 1
                stall_text = generate_stall_text(stalls, date_offset=date_offset)
                await reply_stall_arrangement(reply_token, stall_text, vendors, with_image=(date_offset == 1))
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
