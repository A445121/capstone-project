import logging
import os
from concurrent.futures import ThreadPoolExecutor
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from .services.line_service import process_message_in_background

logger = logging.getLogger(__name__)

channel_secret = os.getenv("LINE_CHANNEL_SECRET")
if not channel_secret:
    raise ValueError("LINE_CHANNEL_SECRET not set")

handler = WebhookHandler(channel_secret)
executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="LineBotWorker")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """
    處理 LINE 的文字訊息事件。
    將訊息處理交由背景執行緒池，以避免 webhook 逾時與資源耗盡。
    """
    try:
        executor.submit(process_message_in_background, event)
    except Exception as e:
        logger.error(f"提交任務到執行緒池失敗: {e}")


def get_handler():
    """回傳已設定的 WebhookHandler 實例。"""
    return handler
