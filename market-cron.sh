#!/bin/bash
# 板橋妙雲宮市集 LINE 自動推播腳本
# 用法：market-cron.sh <endpoint>
# 範例：market-cron.sh send-arrived

ENDPOINT=$1
RENDER_URL="https://poming-linebot.onrender.com"
LOG="/tmp/market-cron.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 觸發 $ENDPOINT" >> "$LOG"

# 喚醒 Render（免費方案閒置後會休眠，最多等 70 秒）
curl -s --max-time 70 "$RENDER_URL/" > /dev/null

# 呼叫端點
result=$(curl -s "$RENDER_URL/$ENDPOINT")
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 結果：$result" >> "$LOG"
