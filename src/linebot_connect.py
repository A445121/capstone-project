import datetime
import functools
import logging
import os
import secrets
import threading  # 保留 threading
import time
from collections import defaultdict


import pyodbc  # 引入 pyodbc 用於捕獲其特定的錯誤
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_talisman import Talisman
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    CarouselColumn,
    CarouselTemplate,
    Configuration,
    MessageAction,
    MessagingApi,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TemplateMessage,
    TextMessage,
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from werkzeug.middleware.proxy_fix import ProxyFix

from database import db  # db 物件現在是 MS SQL Server 的接口
# F401: 下面兩個匯入在此檔案中未使用，通常在 app.py 中調用
# from equipment_scheduler import start_scheduler
# from initial_data import initialize_equipment_data

# 設定 logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 從環境變數取得 LINE 金鑰
channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
channel_secret = os.getenv("LINE_CHANNEL_SECRET")

if not channel_access_token or not channel_secret:
    raise ValueError(
        "LINE 金鑰未正確設置。請確定環境變數 LINE_CHANNEL_ACCESS_TOKEN、LINE_CHANNEL_SECRET 已設定。"
    )

# 判斷是否在測試環境 - 避免在測試期間啟用 Talisman 重定向
is_testing = os.environ.get("TESTING", "False").lower() == "true"

# 密鑰文件路徑可由環境變數覆蓋
SECRET_KEY_FILE = os.getenv("SECRET_KEY_FILE", "data/secret_key.txt")


def get_or_create_secret_key():
    """獲取或創建一個固定的 secret key"""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key

    os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
    try:
        if os.path.exists(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, "r") as f:
                key = f.read().strip()
                if key:
                    return key
        key = secrets.token_hex(24)
        with open(SECRET_KEY_FILE, "w") as f:
            f.write(key)
        return key
    except Exception as e:
        logger.warning(f"無法讀取或寫入密鑰文件: {e}，使用臨時密鑰")
        return secrets.token_hex(24)


# 全局請求計數器與鎖 (線程安全)
request_counts = defaultdict(list)
last_cleanup_time = time.time()
request_counts_lock = threading.Lock()


def cleanup_request_counts():
    """清理長時間未使用的 IP 地址"""
    global last_cleanup_time
    current_time = time.time()

    if current_time - last_cleanup_time < 3600:
        return

    with request_counts_lock:
        ips_to_remove = [
            ip for ip, timestamps in request_counts.items()
            if not timestamps or current_time - max(timestamps) > 3600
        ]
        for ip in ips_to_remove:
            del request_counts[ip]
        last_cleanup_time = current_time
        logger.info("已清理過期請求記錄")


def rate_limit_check(ip, max_requests=30, window_seconds=60):
    """
    簡單的 IP 請求限制，防止暴力攻擊
    """
    current_time = time.time()
    cleanup_request_counts()
    with request_counts_lock:
        request_counts[ip] = [
            timestamp for timestamp in request_counts[ip]
            if current_time - timestamp < window_seconds
        ]
        if len(request_counts[ip]) >= max_requests:
            return False
        request_counts[ip].append(current_time)
        return True


ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "password")


def admin_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates"
        ),
        static_folder=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "static"
        )
    )
    app.secret_key = get_or_create_secret_key()
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    csp = {
        "default-src": "'self'",
        "script-src": ["'self'", "'unsafe-inline'", "https://cdn.powerbi.com"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "img-src": ["'self'", "data:"],
        "frame-src": ["https://app.powerbi.com"],
        "connect-src": [
            "'self'", "https://api.powerbi.com", "https://login.microsoftonline.com"
        ],
    }

    if not is_testing:
        Talisman(
            app,
            content_security_policy=csp,
            content_security_policy_nonce_in=["script-src"],
            force_https=True,
            session_cookie_secure=True,
            session_cookie_http_only=True,
            feature_policy="geolocation 'none'; microphone 'none'; camera 'none'",
        )
    else:
        logger.info("Running in test mode - Talisman security features disabled")
    return app


app = create_app()

configuration = Configuration(access_token=channel_access_token)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)
handler = WebhookHandler(channel_secret)


def register_routes(app_instance):  # 傳入 app 實例
    @app_instance.route("/callback", methods=["POST"])
    def callback():
        signature = request.headers.get("X-Line-Signature")
        body = request.get_data(as_text=True)
        if not signature:
            logger.error("缺少 X-Line-Signature 標頭。")
            abort(400)
        try:
            handler.handle(body, signature)
        except InvalidSignatureError:
            logger.error("無效的簽名")
            abort(400)
        return "OK"

    @app_instance.route("/")
    def index():
        return render_template("index.html")

    @app_instance.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                session["admin_logged_in"] = True
                session.permanent = True  # 可選：使 session 持久
                app_instance.permanent_session_lifetime = datetime.timedelta(days=7)  # 可選：設定持久時間
                return redirect(request.args.get("next") or url_for("admin_dashboard"))
            else:
                flash("登入失敗，請確認帳號密碼是否正確", "error")
        return render_template("admin_login.html")

    @app_instance.route("/admin/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        return redirect(url_for("admin_login"))

    @app_instance.route("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        # 直接使用 db 物件的方法
        conversation_stats = db.get_conversation_stats()
        recent_conversations = db.get_recent_conversations(limit=20)  # 使用 user_id
        system_info = {
            "openai_api_key": "已設置" if os.getenv("OPENAI_API_KEY") else "未設置",
            "line_channel_secret": "已設置" if os.getenv("LINE_CHANNEL_SECRET") else "未設置",
            "db_server": os.getenv("DB_SERVER", "localhost"),
            "db_name": os.getenv("DB_NAME", "conversations")
        }
        return render_template(
            "admin_dashboard.html",
            stats=conversation_stats,
            recent=recent_conversations,  # recent 列表中的 user_id (原 sender_id)
            system_info=system_info,
        )

    @app_instance.route("/admin/conversation/<user_id>")  # 這裡的 user_id 是正確的
    @admin_required
    def admin_view_conversation(user_id):
        # 直接使用 db 物件的方法
        # get_conversation_history 以 user_id (即 sender_id) 查詢
        conversation = db.get_conversation_history(user_id, limit=50)
        user_info = db.get_user_preference(user_id)
        return render_template(
            "admin_conversation.html",
            conversation=conversation,
            user_id=user_id,
            user_info=user_info,
        )

    @app_instance.template_filter("nl2br")
    def nl2br(value):
        if not value:
            return ""
        return value.replace("\n", "<br>")

    @app_instance.context_processor
    def utility_processor():
        def now_func():
            return datetime.datetime.now()
        return dict(now=now_func)


register_routes(app)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    text_lower = text.lower()
    user_id = event.source.user_id  # 獲取 user_id

    db.get_user_preference(user_id)  # 如果不存在，會在 get_user_preference 中創建

    reply_message_obj = None  # 初始化 reply_message_obj
    equipment_name = ""  # 初始化 equipment_name

    if text_lower in ["help", "幫助", "選單", "menu"]:
        quick_reply = QuickReply(
            items=[
                QuickReplyItem(action=MessageAction(label="查看報表", text="powerbi")),
                QuickReplyItem(action=MessageAction(label="訂閱設備", text="訂閱設備")),
                QuickReplyItem(action=MessageAction(label="設備狀態", text="設備狀態")),
                QuickReplyItem(action=MessageAction(label="使用說明", text="使用說明")),
            ]
        )
        reply_message_obj = TextMessage(
            text="您可以選擇以下選項或直接輸入您的問題：", quick_reply=quick_reply
        )

    elif text_lower in ["使用說明", "說明", "教學", "指南", "guide"]:
        carousel_template = CarouselTemplate(
            columns=[
                CarouselColumn(
                    title="如何使用聊天機器人",
                    text="直接輸入您的問題，AI 將為您提供解答。",
                    actions=[
                        MessageAction(label="試試問問題", text="如何建立一個簡單的網頁？")
                    ],
                ),
                CarouselColumn(
                    title="設備監控功能",
                    text="查看半導體設備的狀態和異常警告。",
                    actions=[MessageAction(label="查看設備狀態", text="設備狀態")],
                ),
                CarouselColumn(
                    title="語言設定",
                    text="輸入 'language:語言代碼' 更改語言。\n目前支援：\nlanguage:zh-Hant (繁中)",
                    actions=[MessageAction(label="設定為繁體中文", text="language:zh-Hant")],
                ),
            ]
        )
        reply_message_obj = TemplateMessage(
            alt_text="使用說明", template=carousel_template
        )

    elif text_lower in ["關於", "about"]:
        reply_message_obj = TextMessage(
            text=(
                "這是一個整合 LINE Bot 與 OpenAI 的智能助理，"
                "可以回答您的技術問題、監控半導體設備狀態並展示。"
                "您可以輸入 'help' 查看更多功能。"
            )
        )

    elif text_lower == "language":
        reply_message_obj = TextMessage(
            text=(
                "您可以通過輸入以下命令設置語言：\n\n"
                "language:zh-Hant - 繁體中文"
            )
        )

    elif text_lower.startswith("language:") or text.startswith("語言:"):
        lang_code_input = text.split(":", 1)[1].strip().lower()
        valid_langs = {"zh-hant": "zh-Hant", "zh": "zh-Hant"}
        lang_to_set = valid_langs.get(lang_code_input)

        if lang_to_set:
            if db.set_user_preference(user_id, language=lang_to_set):
                confirmation_map = {"zh-Hant": "語言已切換至 繁體中文"}
                reply_message_obj = TextMessage(
                    text=confirmation_map.get(lang_to_set, f"語言已設定為 {lang_to_set}")
                )
            else:
                reply_message_obj = TextMessage(text="語言設定失敗，請稍後再試。")
        else:
            reply_message_obj = TextMessage(
                text="不支援的語言代碼。目前支援：zh-Hant (繁體中文)"
            )

    elif text_lower in ["設備狀態", "機台狀態", "equipment status"]:
        try:
            with db._get_connection() as conn:  # 使用 MS SQL Server 連線
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT e.type, COUNT(*) as total,
                           SUM(CASE WHEN e.status = 'normal' THEN 1 ELSE 0 END) as normal_count,
                           SUM(CASE WHEN e.status = 'warning' THEN 1 ELSE 0 END) as warning_count,
                           SUM(CASE WHEN e.status = 'critical' THEN 1 ELSE 0 END) as critical_count,
                           SUM(CASE WHEN e.status = 'emergency' THEN 1 ELSE 0 END) as emergency_count,
                           SUM(CASE WHEN e.status = 'offline' THEN 1 ELSE 0 END) as offline_count
                    FROM equipment e
                    GROUP BY e.type;
                    """
                )
                stats = cursor.fetchall()
                if not stats:
                    reply_message_obj = TextMessage(text="目前尚未設定任何設備。")
                else:
                    response_text = "設備狀態摘要：\n\n"
                    for row in stats:
                        eq_type_db, total, normal, warning, critical, emergency, offline = row
                        type_name = {
                            "dicer": "切割機"
                        }.get(eq_type_db, eq_type_db)
                        response_text += f"{type_name}：總數 {total}, 正常 {normal}"
                        if warning > 0:
                            response_text += f", 警告 {warning}"
                        if critical > 0:
                            response_text += f", 嚴重 {critical}"
                        if emergency > 0:
                            response_text += f", 緊急 {emergency}"
                        if offline > 0:
                            response_text += f", 離線 {offline}"
                        response_text += "\n"

                    cursor.execute(
                        """
                        SELECT TOP 5 e.name, e.type, e.status, e.equipment_id,
                                     ah.alert_type, ah.created_at
                        FROM equipment e
                        LEFT JOIN alert_history ah ON e.equipment_id = ah.equipment_id
                            AND ah.is_resolved = 0
                            AND ah.id = (
                                SELECT MAX(ah_inner.id)
                                FROM alert_history ah_inner
                                WHERE ah_inner.equipment_id = e.equipment_id AND ah_inner.is_resolved = 0
                            )
                        WHERE e.status NOT IN ('normal', 'offline')
                        ORDER BY CASE e.status
                            WHEN 'emergency' THEN 1
                            WHEN 'critical' THEN 2
                            WHEN 'warning' THEN 3
                            ELSE 4
                        END, ah.created_at DESC;
                        """
                    )
                    abnormal_equipments = cursor.fetchall()
                    if abnormal_equipments:
                        response_text += "\n⚠️ 近期異常設備 (最多5筆)：\n\n"
                        for name_db, eq_type, status, eq_id, alert_t, alert_time in abnormal_equipments:
                            type_name = {
                                "dicer": "切割機"
                            }.get(eq_type, eq_type)
                            status_emoji = {
                                "warning": "⚠️", "critical": "🔴", "emergency": "🚨"
                            }.get(status, "❓")
                            response_text += (
                                f"{name_db} ({type_name}) 狀態: {status_emoji} {status}\n"
                            )
                            if alert_t and alert_time:
                                response_text += (
                                    f"  最新警告: {alert_t} "
                                    f"於 {alert_time.strftime('%Y-%m-%d %H:%M')}\n"
                                )
                        response_text += "\n輸入「設備詳情 [設備名稱]」可查看更多資訊。"
                    reply_message_obj = TextMessage(text=response_text)
        except pyodbc.Error as db_err:
            logger.error(f"取得設備狀態失敗 (MS SQL Server): {db_err}")
            reply_message_obj = TextMessage(text="取得設備狀態失敗，請稍後再試。")
        except Exception as e:
            logger.error(f"處理設備狀態查詢時發生未知錯誤: {e}")
            reply_message_obj = TextMessage(text="系統忙碌中，請稍候再試。")

    elif text_lower.startswith("設備詳情") or text_lower.startswith("機台詳情"):
        command_parts = text.split(" ", 1)
        if len(command_parts) < 2 or not command_parts[1].strip():
            command_parts_zh = text.split(" ", 1)  # E701: 全形空格問題已在此解決
            if len(command_parts_zh) < 2 or not command_parts_zh[1].strip():
                reply_message_obj = TextMessage(
                    text="請指定設備名稱或ID，例如「設備詳情 黏晶機A1」或「設備詳情 DB001」"
                )
            else:
                equipment_name = command_parts_zh[1].strip()
        else:
            equipment_name = command_parts[1].strip()

        if equipment_name:  # 確保 equipment_name 已被賦值
            try:
                with db._get_connection() as conn:  # 使用 MS SQL Server 連線
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT e.equipment_id, e.name, e.type, e.status,
                               e.location, e.last_updated
                        FROM equipment e
                        WHERE e.name LIKE ? OR e.equipment_id = ?;
                        """,
                        (f"%{equipment_name}%", equipment_name.upper())
                    )
                    equipment = cursor.fetchone()
                    if not equipment:
                        reply_message_obj = TextMessage(
                            text=f"查無設備「{equipment_name}」的資料。"
                        )
                    else:
                        eq_id, name_db, eq_type, status, location, last_updated_db = equipment
                        type_name = {
                            "dicer": "切割機"
                        }.get(eq_type, eq_type)
                        status_emoji = {
                            "normal": "✅", "warning": "⚠️", "critical": "🔴",
                            "emergency": "🚨", "offline": "⚫"
                        }.get(status, "❓")
                        last_updated_str = (
                            last_updated_db.strftime('%Y-%m-%d %H:%M:%S')
                            if last_updated_db else '未記錄'
                        )
                        response_text = (
                            f"設備詳情： {name_db} ({eq_id})\n"
                            f"類型: {type_name}\n"
                            f"狀態: {status_emoji} {status}\n"
                            f"地點: {location or '未提供'}\n"
                            f"最後更新: {last_updated_str}\n\n"
                        )
                        cursor.execute(
                            """
                            WITH RankedMetrics AS (
                                SELECT
                                    em.metric_type, em.value, em.unit, em.timestamp,
                                    ROW_NUMBER() OVER(
                                        PARTITION BY em.metric_type ORDER BY em.timestamp DESC
                                    ) as rn
                                FROM equipment_metrics em
                                WHERE em.equipment_id = ?
                            )
                            SELECT metric_type, value, unit, timestamp
                            FROM RankedMetrics
                            WHERE rn = 1
                            ORDER BY metric_type;
                            """, (eq_id,)
                        )
                        metrics = cursor.fetchall()
                        if metrics:
                            response_text += "📊 最新監測值：\n"
                            for metric_t, val, unit, ts in metrics:
                                response_text += (
                                    f"  {metric_t}: {val:.2f} {unit or ''} "
                                    f"({ts.strftime('%H:%M:%S')})\n"
                                )
                        else:
                            response_text += "暫無最新監測指標。\n"
                        cursor.execute(
                            """
                            SELECT TOP 3 alert_type, severity, created_at, message
                            FROM alert_history
                            WHERE equipment_id = ? AND is_resolved = 0
                            ORDER BY created_at DESC;
                            """, (eq_id,)
                        )
                        alerts = cursor.fetchall()
                        if alerts:
                            response_text += "\n⚠️ 未解決的警報：\n"
                            for alert_t, severity, alert_time, _ in alerts:  # msg_content not used
                                sev_emoji = {
                                    "warning": "⚠️", "critical": "🔴", "emergency": "🚨"
                                }.get(severity, "ℹ️")
                                response_text += (
                                    f"  {sev_emoji} {alert_t} ({severity}) "
                                    f"於 {alert_time.strftime('%Y-%m-%d %H:%M')}\n"
                                )
                        else:
                            response_text += "\n目前無未解決的警報。\n"
                        cursor.execute(
                            """
                            SELECT TOP 1 operation_type, start_time, lot_id, product_id
                            FROM equipment_operation_logs
                            WHERE equipment_id = ? AND end_time IS NULL
                            ORDER BY start_time DESC;
                            """, (eq_id,)
                        )
                        operation = cursor.fetchone()
                        if operation:
                            op_t, start_t, lot, prod = operation
                            response_text += "\n🔄 目前運行中的作業：\n"
                            response_text += (
                                f"  作業類型: {op_t}\n"
                                f"  開始時間: {start_t.strftime('%Y-%m-%d %H:%M')}\n"
                            )
                            if lot:
                                response_text += f"  批次: {lot}\n"
                            if prod:
                                response_text += f"  產品: {prod}\n"
                        else:
                            response_text += "\n目前無運行中的作業。\n"
                        reply_message_obj = TextMessage(text=response_text.strip())
            except pyodbc.Error as db_err:
                logger.error(f"取得設備詳情失敗 (MS SQL Server): {db_err}")
                reply_message_obj = TextMessage(text="取得設備詳情失敗，請稍後再試。")
            except Exception as e:
                logger.error(f"處理設備詳情查詢時發生未知錯誤: {e}")
                reply_message_obj = TextMessage(text="系統忙碌中，請稍候再試。")

    #這裡曾經有用戶訂閱功能 詳情請在 PR -> del(equipment_monitor.py , equipment_scheduler.py) chare(linebot_connect.py) 移除我的訂閱以及切割機 項目中找回資料
    #因為沒有廠商提供資料
    #請每一個貢獻者都注意這個功能已經被移除

    else:  # 預設：從 OpenAI (main.py) 取得回應
        try:
            from src.main import reply_message as main_reply_message
            response_text = main_reply_message(event)
            reply_message_obj = TextMessage(text=response_text)
        except ImportError:
            logger.error("無法導入 src.main.reply_message")
            reply_message_obj = TextMessage(text="抱歉，AI 對話功能暫時無法使用。")
        except Exception as e:
            logger.error(f"調用 OpenAI 回覆訊息失敗: {e}")
            reply_message_obj = TextMessage(
                text="抱歉，處理您的請求時發生了錯誤，AI 功能可能暫時無法使用。"
            )

    if reply_message_obj:
        try:
            reply_request = ReplyMessageRequest(
                reply_token=event.reply_token, messages=[reply_message_obj]
            )
            line_bot_api.reply_message_with_http_info(reply_request)
        except Exception as e:
            logger.error(f"最終回覆訊息失敗: {e}")
    else:
        logger.info(f"未處理的訊息: {text} from user {user_id}")
        unknown_command_reply = TextMessage(
            text="抱歉，我不太明白您的意思。您可以輸入 'help' 查看我能做些什麼。"
        )
        try:
            reply_request = ReplyMessageRequest(
                reply_token=event.reply_token, messages=[unknown_command_reply]
            )
            line_bot_api.reply_message_with_http_info(reply_request)
        except Exception as e:
            logger.error(f"發送未知命令回覆失敗: {e}")


def send_notification(user_id_to_notify, message_text):
    """發送 LINE 訊息給特定使用者"""
    try:
        message_obj = TextMessage(text=message_text)
        push_request = PushMessageRequest(to=user_id_to_notify, messages=[message_obj])
        line_bot_api.push_message_with_http_info(push_request)
        logger.info(f"通知已成功發送給 user_id: {user_id_to_notify}")
        return True
    except Exception as e:
        logger.error(f"發送通知給 user_id {user_id_to_notify} 失敗: {e}")
        return False


if __name__ == "__main__":
    logger.info("linebot_connect.py 被直接執行。建議透過 app.py 啟動應用程式。")
