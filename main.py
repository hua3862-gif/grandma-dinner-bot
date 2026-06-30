import os
import logging
from datetime import datetime, timedelta
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

# 2. LINE 憑證設定
CHANNEL_SECRET = "e62b1fccd5bc395a1ed19bd7c7015b92"
CHANNEL_ACCESS_TOKEN = "v8ticoY2QbrUdRcQur6CyKorrAwgOVZDWiJuGMILlB8DlYrV8iIvwTJschTzalb9iOofv2cVaQn+PEcVPmpSjxz0t3YX151hhvU5M04SWh316K7PuiZATSsoXDEhwpDklyw0tJV9pUSI4J6rd6ylnwdB04t89/1O/w1cDnyilFU="

# 3. 目標 ID 設定
SURVEY_TARGET_ID = "Cacfb77eec8efc921e271c78c8a6b843c"
REPORT_TARGET_ID = "C80c5607b6b214a6e7e5b31d67440a796"

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 4. 資料儲存庫
dinner_records = {}

# 5. 固定家庭成員名單
FIXED_MEMBERS = {
    "U585d02d08fe8913778c48379ad64f8a6": "瑋勵bos",
    "Ub46ef25cf97524c6d86f60fea1cc0af9": "瓊瑛pemilik rumah kontrakan",
    "U3ba83a8c3e64f841d973243d2e2fa57f": "子晴saudari",
    "U9155cb922c0498f86a2d2f2bb528b97e": "小喬adik"
}

# 🌟 核心邏輯設定：每天固定不回家吃飯、不帶便當的人
ABSENT_USER_ID = "Ub46ef25cf97524c6d86f60fea1cc0af9"
ABSENT_NAME = "瓊瑛pemilik rumah kontrakan"

# ==========================================
# 🌟 統一假日判定函式
# ==========================================
def is_today_holiday():
    """判定今天是不是放假日（週末或國定假日）"""
    today = datetime.now()
    today_weekday = today.weekday() # 0=週一, ..., 6=週日
    today_str = today.strftime("%Y-%m-%d")
    
    if today_weekday in [5, 6]:
        return True
        
    taiwan_holidays = [
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", 
        "2026-02-23", "2026-02-27", "2026-04-03", "2026-04-06", "2026-06-19", "2026-09-25",
    ]
    if today_str in taiwan_holidays:
        return True
        
    return False

# ==========================================
# 精準判斷「明天」需不需要帶便當
# ==========================================
def should_send_bento_tomorrow():
    """判定明天是否需要帶便當（如果明天是週末或國定假日，今天點晚餐就不續問便當）"""
    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    tomorrow_weekday = tomorrow.weekday() # 0=週一, ..., 6=週日
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    
    if tomorrow_weekday in [5, 6]:
        return False

    taiwan_holidays = [
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", 
        "2026-02-23", "2026-02-27", "2026-04-03", "2026-04-06", "2026-06-19", "2026-09-25",
    ]
    if tomorrow_str in taiwan_holidays:
        return False
        
    taiwan_makeup_workdays = [
        "2026-02-07",
    ]
    if tomorrow_str in taiwan_makeup_workdays:
        return True  
        
    return True

# ==========================================
# 生成精美的問卷卡片 (Flex Message)
# ==========================================
def create_dinner_card():
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
# 【定時任務】問卷發送
# ==========================================
def send_daily_survey():
    """中午 12:00 發送晚餐問卷 (放假日絕對不啟動)"""
    global dinner_records
    if is_today_holiday():
        logger.info("今天為放假日（週末或國定假日），完全跳過晚餐調查。")
        return
        
    logger.info("觸發晚餐問卷調查...")
    
    # 🌟 啟動調查時，背後自動幫固定不吃的成員填妥紀錄
    if ABSENT_USER_ID:
        dinner_records[ABSENT_USER_ID] = {"name": ABSENT_NAME, "dinner": "不留飯", "bento": "不要"}
        logger.info(f"🤖 系統已自動將 {ABSENT_NAME} 登記為不留飯與不需要便當。")

    if SURVEY_TARGET_ID.startswith("C") or SURVEY_TARGET_ID.startswith("R") or SURVEY_TARGET_ID.startswith("U"):
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            flex_card = create_dinner_card()
            flex_message = FlexMessage(alt_text="🍴 晚餐調查開始囉！", contents=flex_card)
            line_bot_api.push_message(PushMessageRequest(to=SURVEY_TARGET_ID, messages=[flex_message]))

# ==========================================
# 統計並發送大廚報告（16:00 視覺強調終極版）
# ==========================================
def report_to_chef():
    global dinner_records
    
    if is_today_holiday():
        logger.info("今天為放假日（週末或國定假日），完全跳過大廚回報。")
        return
        
    logger.info("開始執行大廚回報...")
    if not REPORT_TARGET_ID.startswith("C") and not REPORT_TARGET_ID.startswith("R") and not REPORT_TARGET_ID.startswith("U"):
        return

    if ABSENT_USER_ID:
        dinner_records[ABSENT_USER_ID] = {"name": ABSENT_NAME, "dinner": "不留飯", "bento": "不要"}

    on_time_list = []
    late_list = []
    no_dinner_list = []
    bento_yes_list = []
    bento_no_list = []
    replied_users = set()

    for uid, info in dinner_records.items():
        name = info["name"]
        replied_users.add(uid)
        
        if info.get("dinner") == "準時": on_time_list.append(name)
        elif info.get("dinner") == "晚一點": late_list.append(name)
        elif info.get("dinner") == "不留飯": no_dinner_list.append(name)
        
        if info.get("bento") == "要": bento_yes_list.append(name)
        elif info.get("bento") == "不要": bento_no_list.append(name)

    total_dinner_count = len(on_time_list) + len(late_list)
    unreplied_list = [name for uid, name in FIXED_MEMBERS.items() if uid not in replied_users]

    report_text = f"📋 【今日晚餐與明日便當報告】 ({datetime.now().strftime('%m/%d')})\n\n"
    
    report_text += f"🏠 晚餐統計 Statistik makan malam：\n"
    report_text += f"⭐ 回家吃飯總人數 Total orang yang pulang makan：\n"
    report_text += f"👉 👑 【 {total_dinner_count} 人 】 👑 👈\n"
    report_text += f"   • 準時到家 Pulang tepat waktu ({len(on_time_list)}人)：{', '.join(on_time_list) if on_time_list else '無'}\n"
    report_text += f"   • 晚一點到 Datang lambat ({len(late_list)}人)：{', '.join(late_list) if late_list else '無'}\n"
    report_text += f" ❌ 不用留飯 Tidak perlu makanan ({len(no_dinner_list)}人)：{', '.join(no_dinner_list) if no_dinner_list else '無'}\n\n"
    
    if not should_send_bento_tomorrow():
        report_text += f"🍱 明日便當統計：\n 💡 明日放假，不進行便當統計。\n\n"
    else:
        report_text += f"🍱 明日便當統計 Statistik Bento Besok：\n"
        report_text += f"⭐ 明日需要便當總人數 Butuh bento：\n"
        report_text += f"👉 🍱 【 {len(bento_yes_list)} 人 】 🍱 👈\n"
        report_text += f" ⭕ 需要便當 Butuh bento ({len(bento_yes_list)}人)：{', '.join(bento_yes_list) if bento_yes_list else '無'}\n"
        report_text += f" ❌ 不需要者 Tidak butuh ({len(bento_no_list)}人)：{', '.join(bento_no_list) if bento_no_list else '無'}\n\n"
    
    report_text += f"⚠️ 尚未回覆人員 Belum menanggapi：\n"
    report_text += f" 🕒 {', '.join(unreplied_list) if unreplied_list else '大家皆已回覆完畢！'}"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(PushMessageRequest(to=REPORT_TARGET_ID, messages=[TextMessage(text=report_text)]))

def clear_records():
    """每天晚上 19:00 自動清空當日登記資料"""
    global dinner_records
    dinner_records.clear()
    logger.info("今日晚餐及便當資料已重置。")

# ==========================================
# 啟動定時任務（12:00 / 16:00 / 19:00）
# ==========================================
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(send_daily_survey, CronTrigger(hour=12, minute=0, timezone="Asia/Taipei"))
scheduler.add_job(report_to_chef, CronTrigger(hour=16, minute=0, timezone="Asia/Taipei"))
scheduler.add_job(clear_records, CronTrigger(hour=19, minute=0, timezone="Asia/Taipei"))
scheduler.start()

# ==========================================
# Webhook 接收節點
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
# 接收並記錄按鈕點擊
# ==========================================
@handler.add(PostbackEvent)
def handle_postback(event: PostbackEvent):
    global dinner_records
    data = event.postback.data
    user_id = event.source.user_id
    
    user_name = FIXED_MEMBERS.get(user_id)
    if not user_name:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            try:
                profile = line_bot_api.get_profile(user_id)
                user_name = profile.display_name
            except Exception:
                user_name = "神秘家人"
    
    if user_id not in dinner_records:
        dinner_records[user_id] = {"name": user_name, "dinner": "未填", "bento": "未填"}

    params = dict(param.split("=") for param in data.split("&"))
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if "dinner" in params:
            dinner_records[user_id]["dinner"] = params["dinner"]
            logger.info(f"📢 [收到回報] {user_name} 登記晚餐：【{params['dinner']}】")
            
            if not should_send_bento_tomorrow():
                dinner_records[user_id]["bento"] = "不需便當"
                final_text = f"👌 已幫您登記晚餐：【{params['dinner']}】。\n🎉 感謝回報！（因明日放假，不統計便當）"
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=final_text)]))
            else:
                reply_text = f"👌 已幫您登記晚餐：【{params['dinner']}】。接下來請選擇明天的便當需求 👇"
                bento_card = create_bento_card()
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(text=reply_text),
                            FlexMessage(alt_text="🍱 續問：明天要帶便當嗎？", contents=bento_card)
                        ]
                    )
                )
            
        elif "bento" in params:
            dinner_records[user_id]["bento"] = params["bento"]
            current_dinner = dinner_records[user_id]["dinner"]
            
            logger.info(f"📢 [收到回報] {user_name} 登記便當：【{params['bento']}】")
            logger.info(f"📊 當前記憶體暫存 (dinner_records): {dinner_records}") # 👈 修正：移除 json.dumps 避免閃退
            
            final_text = f"🎉 感謝回報！\n今日晚餐：【{current_dinner}】\n明日便當：【{params['bento']}帶便當】"
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=final_text)]))

# ==========================================
# 即時進度查詢
# ==========================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = event.message.text.strip()
    user_id = event.source.user_id

    if text == "進度":
        if ABSENT_USER_ID:
            dinner_records[ABSENT_USER_ID] = {"name": ABSENT_NAME, "dinner": "不留飯", "bento": "不要"}

        on_time = []
        late = []
        no_dinner = []
        bento_yes = []
        bento_no = []
        replied = set()
        
        for uid, info in dinner_records.items():
            name = info["name"]
            replied.add(uid)
            if info.get("dinner") == "準時": on_time.append(name)
            elif info.get("dinner") == "晚一點": late.append(name)
            elif info.get("dinner") == "不留飯": no_dinner.append(name)
            
            if info.get("bento") == "要": bento_yes.append(name)
            elif info.get("bento") == "不要": bento_no.append(name)
            
        unreplied = [name for uid, name in FIXED_MEMBERS.items() if uid not in replied]
        
        status_text = f"🔍 【當前晚餐回報進度】\n\n"
        status_text += f"🏡 晚餐統計：\n"
        status_text += f"👉 👑 【 {len(on_time) + len(late)} 人 吃飯 】 👑 👈\n"
        status_text += f" • 準時 ({len(on_time)}人)：{', '.join(on_time) if on_time else '無'}\n"
        status_text += f" • 晚點 ({len(late)}人)：{', '.join(late) if late else '無'}\n"
        status_text += f" • 不吃 ({len(no_dinner)}人)：{', '.join(no_dinner) if no_dinner else '無'}\n\n"
        
        if should_send_bento_tomorrow():
            status_text += f"🍱 便當統計：\n"
            status_text += f"👉 🍱 【 {len(bento_yes)} 人 帶便當 】 🍱 👈\n"
            status_text += f" • 需要 ({len(bento_yes)}人)：{', '.join(bento_yes) if bento_yes else '無'}\n"
            status_text += f" • 不要 ({len(bento_no)}人)：{', '.join(bento_no) if bento_no else '無'}\n\n"
        
        status_text += f"⚠️ 尚未回覆 ({len(unreplied)}人)：\n"
        status_text += f" • {', '.join(unreplied) if unreplied else '大家都填寫完畢囉！'}"
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=status_text)]))
            return

    if text.upper() in ["ID", "帳號"]:
        source_type = event.source.type
        reply_id_text = f"👥 本群組 ID：\n{event.source.group_id}" if source_type == "group" else f"👤 您的個人 ID：\n{user_id}"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_id_text)]))

from fastapi.responses import JSONResponse

@app.get("/")
@app.head("/")
def read_root():
    return JSONResponse(content={"status": "完美升級版晚餐與便當調查機器人運作中！"})
