Python
import os
import logging
from datetime import datetime
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

# LINE Bot SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, PostbackEvent, TextMessageContent

# 定時任務套件
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# 1. 基礎設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

# 2. LINE 憑證設定 (請填入您的憑證密碼)
CHANNEL_SECRET = "e62b1fccd5bc395a1ed19bd7c7015b92"
CHANNEL_ACCESS_TOKEN = "v8ticoY2QbrUdRcQur6CyKorrAwgOVZDWiJuGMILlB8DlYrV8iIvwTJschTzalb9iOofv2cVaQn+PEcVPmpSjxz0t3YX151hhvU5M04SWh316K7PuiZATSsoXDEhwpDklyw0tJV9pUSI4J6rd6ylnwdB04t89/1O/w1cDnyilFU="

# 3. 目標 ID 設定
SURVEY_TARGET_ID = "請填入家庭大群組的ID"
REPORT_TARGET_ID = "請填入回報群組或大廚的個人ID"

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 4. 資料儲存庫
# 結構: { "user_id": { "name": "王小明", "dinner": "準時/晚一點/不留飯", "bento": "要/不要", "last_seen": datetime } }
dinner_records = {}
# 用於動態記錄群組內所有已知成員的名單（用來比對誰未回覆）
known_members = {}

# ==========================================
# 【新增功能】判斷今天是否為台灣上班日
# ==========================================
def is_taiwan_workday():
    today = datetime.now()
    weekday = today.weekday() # 0=週一, ..., 5=週六, 6=週日
    
    # 1. 基本週末判定 (週六=5, 週日=6)
    is_weekend = weekday in [5, 6]
    
    # 2. 這裡預留手動調整空間 (例如國定假日放假、或是週六需要補班)
    # 格式為 "YYYY-MM-DD"
    taiwan_holidays = [
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", # 範例：春節
        "2026-02-23", "2026-02-27", "2026-04-03", "2026-04-06", "2026-06-19", "2026-09-25",
    ]
    taiwan_makeup_workdays = [
        "2026-02-07", # 範例：週六補班日
    ]
    
    date_str = today.strftime("%Y-%m-%d")
    
    if date_str in taiwan_makeup_workdays:
        return True  # 雖然是週末，但要補班，所以算上班日
    if date_str in taiwan_holidays:
        return False # 雖然是週間，但放假，不算上班日
        
    return not is_weekend

# ==========================================
# 【優化功能】生成精美的問卷卡片 (Flex Message)
# ==========================================
def create_dinner_card():
    """第一階段：問晚餐"""
    flex_json = {
      "type": "bubble", "size": "giga",
      "header": {
        "type": "box", "layout": "vertical", "backgroundColor": "#FF6B6B",
        "contents": [{"type": "text", "text": "🏠 今晚回家吃飯調查", "weight": "bold", "color": "#FFFFFF", "size": "xl", "align": "center"}]
      },
      "body": {
        "type": "box", "layout": "vertical",
        "contents": [{"type": "text", "text": "今天下班/下課要回家吃晚餐嗎？請點選下方選項：", "wrap": True, "color": "#555555", "size": "md"}]
      },
      "footer": {
        "type": "box", "layout": "vertical", "spacing": "sm",
        "contents": [
          {"type": "button", "style": "primary", "color": "#2ECC71", "action": {"type": "postback", "label": "🙋‍♂️ 我要回家吃 - 準時", "data": "dinner=準時"}},
          {"type": "button", "style": "primary", "color": "#3498DB", "action": {"type": "postback", "label": "🕒 我要回家吃 - 晚一點", "data": "dinner=晚一點"}},
          {"type": "button", "style": "primary", "color": "#95A5A6", "action": {"type": "postback", "label": "🙅‍♂️ 不用留我的飯", "data": "dinner=不留飯"}}
        ]
      }
    }
    return FlexContainer.from_dict(flex_json)

def create_bento_card():
    """第二階段：問便當"""
    flex_json = {
      "type": "bubble", "size": "giga",
      "header": {
        "type": "box", "layout": "vertical", "backgroundColor": "#8E44AD",
        "contents": [{"type": "text", "text": "🍱 明天便當調查", "weight": "bold", "color": "#FFFFFF", "size": "xl", "align": "center"}]
      },
      "body": {
        "type": "box", "layout": "vertical",
        "contents": [{"type": "text", "text": "順便統計一下，明天需要為您準備帶便當嗎？", "wrap": True, "color": "#555555", "size": "md"}]
      },
      "footer": {
        "type": "box", "layout": "vertical", "spacing": "sm",
        "contents": [
          {"type": "button", "style": "primary", "color": "#E67E22", "action": {"type": "postback", "label": "🍱 明天要帶便當", "data": "bento=要"}},
          {"type": "button", "style": "primary", "color": "#BDC3C7", "action": {"type": "postback", "label": "❌ 不需要便當", "data": "bento=不要"}}
        ]
      }
    }
    return FlexContainer.from_dict(flex_json)

# ==========================================
# 【定時任務】排程管理 (僅在台灣上班日發送)
# ==========================================
def send_daily_survey():
    """【自動功能 1】每天下午 15:30 發送晚餐問卷 (過濾假節日)"""
    if not is_taiwan_workday():
        logger.info("今天不是台灣上班日，跳過發送問卷。")
        return
        
    logger.info("上班日發送晚餐問卷調查...")
    if SURVEY_TARGET_ID.startswith("C") or SURVEY_TARGET_ID.startswith("R") or SURVEY_TARGET_ID.startswith("U"):
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            flex_card = create_dinner_card()
            flex_message = FlexMessage(alt_text="📢 晚餐調查開始囉！", container=flex_card)
            line_bot_api.push_message(PushMessageRequest(to=SURVEY_TARGET_ID, messages=[flex_message]))

def report_to_chef():
    """【自動功能 2】每天下午 17:00 統計名單（包含未回覆），主動傳給大廚"""
    if not is_taiwan_workday():
        return
        
    logger.info("開始執行上班日大廚回報...")
    if not REPORT_TARGET_ID.startswith("C") and not REPORT_TARGET_ID.startswith("R") and not REPORT_TARGET_ID.startswith("U"):
        return
        
    on_time_list = []
    late_list = []
    no_dinner_list = []
    bento_yes_list = []
    bento_no_list = []
    replied_users = set()

    for uid, info in dinner_records.items():
        name = info["name"]
        replied_users.add(uid)
        
        # 晚餐分類
        if info.get("dinner") == "準時": on_time_list.append(name)
        elif info.get("dinner") == "晚一點": late_list.append(name)
        elif info.get("dinner") == "不留飯": no_dinner_list.append(name)
        
        # 便當分類
        if info.get("bento") == "要": bento_yes_list.append(name)
        elif info.get("bento") == "不要": bento_no_list.append(name)

    # 找出未回覆的人 (比對已知在群組裡點過按鈕或發過言的成員名單)
    unreplied_list = [name for uid, name in known_members.items() if uid not in replied_users]

    # 彙整報告文字
    report_text = f"📋 【今日晚餐與明日便當報告Laporan makan malam hari ini dan laporan bento untuk besok】 ({datetime.now().strftime('%m/%d')})\n\n"
    report_text += f"🏠 晚餐統計Statistik makan malam：\n"
    report_text += f" 👥 回家吃飯總人數Jumlah total orang yang makan di rumah：{len(on_time_list) + len(late_list)} 人\n"
    report_text += f"   • 準時到家Pulang ke rumah tepat waktu：{', '.join(on_time_list) if on_time_list else '無'}\n"
    report_text += f"   • 晚一點到Datang lebih lambat：{', '.join(late_list) if late_list else '無'}\n"
    report_text += f" ❌ 不用留飯Tidak perlu menyimpan makanan：{', '.join(no_dinner_list) if no_dinner_list else '無'}\n\n"
    
    report_text += f"🍱 明日便當統計Statistik Bento Besok：\n"
    report_text += f" ⭕ 需要便當Butuh kotak bento：{', '.join(bento_yes_list) if bento_yes_list else '無'}\n"
    report_text += f" ❌ 不需要者Mereka yang tidak membutuhkan：{', '.join(bento_no_list) if bento_no_list else '無'}\n\n"
    
    report_text += f"⚠️ 尚未回覆人員Personel yang belum memberikan tanggapan：\n"
    report_text += f" 🕒 {', '.join(unreplied_list) if unreplied_list else '全數皆已回覆Semua pertanyaan telah dijawab！'}"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(PushMessageRequest(to=REPORT_TARGET_ID, messages=[TextMessage(text=report_text)]))

def clear_records():
    """【自動功能 3】每天晚上 19:30 自動清空當日登記，但保留已知成員清單"""
    global dinner_records
    dinner_records.clear()
    logger.info("今日晚餐及便當資料已重置。")

# 啟動定時任務
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(send_daily_survey, CronTrigger(hour=16, minute=00))
scheduler.add_job(report_to_chef, CronTrigger(hour=16, minute=5))
scheduler.add_job(clear_records, CronTrigger(hour=19, minute=30))
scheduler.start()

# ==========================================
# 【接收節點】Webhook
# ==========================================
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    try:
        handler.handle(body_str, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid Signature")
    return PlainTextResponse("OK")

# ==========================================
# 【事件處理】接收並記錄按鈕點擊
# ==========================================
@handler.add(PostbackEvent)
def handle_postback(event: PostbackEvent):
    global dinner_records, known_members
    data = event.postback.data
    user_id = event.source.user_id
    
    # 抓取真實暱稱並存入已知名單
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(user_id)
            user_name = profile.display_name
        except Exception:
            user_name = "神秘家人"
    
    known_members[user_id] = user_name
    if user_id not in dinner_records:
        dinner_records[user_id] = {"name": user_name, "dinner": "未填", "bento": "未填"}

    # 解析點擊的資料
    params = dict(param.split("=") for param in data.split("&"))
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        # 狀況 A：填寫完晚餐 ➔ 立刻挑出詢問便當的卡片
        if "dinner" in params:
            dinner_records[user_id]["dinner"] = params["dinner"]
            reply_text = f"👌 已幫您登記晚餐：【{params['dinner']}】。接下來請選擇明天的便當需求 👇"
            
            bento_card = create_bento_card()
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=reply_text),
                        FlexMessage(alt_text="🍱 續問：明天要帶便當嗎？", container=bento_card)
                    ]
                )
            )
            
        # 狀況 B：填寫完便當 ➔ 悄悄話收尾
        elif "bento" in params:
            dinner_records[user_id]["bento"] = params["bento"]
            current_dinner = dinner_records[user_id]["dinner"]
            final_text = f"🎉 感謝回報！\n今日晚餐：【{current_dinner}】\n明日便當：【{params['bento']}帶便當】"
            
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=final_text)]
                )
            )

# ==========================================
# 【貼心工具】自動收集名單與查 ID 
# ==========================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    global known_members
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    # 只要有人發言，順便記錄他的名字以便納入「未回覆人員」的追蹤基礎名單中
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(user_id)
            known_members[user_id] = profile.display_name
        except Exception:
            pass

    # 口令查 ID
    if text.upper() in ["ID", "帳號"]:
        source_type = event.source.type
        reply_id_text = f"👥 本群組 ID：\n{event.source.group_id}" if source_type == "group" else f"👤 您的個人 ID：\n{user_id}"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_id_text)]))

@app.get("/")
def read_root():
    return {"status": "完美升級版晚餐與便當調查機器人運作中！"}
