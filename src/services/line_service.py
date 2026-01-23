import os
import logging
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
    MulticastRequest,
)
from linebot.v3.messaging.exceptions import ApiException
from ..database import db
from src.main import reply_message as main_reply_message

logger = logging.getLogger(__name__)

channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not channel_access_token:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN not set")

configuration = Configuration(access_token=channel_access_token)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)


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


def send_multicast_notification(user_ids_to_notify, message_text):
    """
    發送 LINE 訊息給多個使用者 (Multicast)。
    這比多次調用 send_notification 更有效率，因為它只發送一次 HTTP 請求。
    """
    if not user_ids_to_notify:
        return True

    try:
        # LINE 多播（Multicast）最多支援 500 個使用者 ID

        message_obj = TextMessage(text=message_text)
        multicast_request = MulticastRequest(to=user_ids_to_notify, messages=[message_obj])
        line_bot_api.multicast(multicast_request)
        logger.info(f"Multicast 通知已成功發送給 {len(user_ids_to_notify)} 位使用者")
        return True
    except Exception as e:
        logger.error(f"發送 Multicast 通知失敗: {e}")
        return False


def send_reply_with_fallback(event, message_obj, user_id):
    """嘗試回覆訊息，若 reply token 無效則改用 Push 模式"""
    messages = [message_obj]
    try:
        reply_request = ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=messages,
        )
        line_bot_api.reply_message_with_http_info(reply_request)
        return True
    except ApiException as api_exc:
        error_body = getattr(api_exc, "body", "") or ""
        combined_error = f"{error_body} {api_exc}".lower()
        logger.error(f"最終回覆訊息失敗: {api_exc}")
        if api_exc.status == 400 and "invalid reply token" in combined_error:
            logger.warning(
                "Reply token 無效，改以 push 訊息方式通知使用者 %s",
                user_id,
            )
            try:
                push_request = PushMessageRequest(to=user_id, messages=messages)
                line_bot_api.push_message_with_http_info(push_request)
                logger.info("Push 訊息成功，使用者 %s", user_id)
                return True
            except Exception as push_exc:
                logger.error(
                    "回覆失敗且推播同樣失敗，使用者 %s，錯誤: %s",
                    user_id,
                    push_exc,
                )
                return False
        return False
    except Exception as exc:
        logger.error(f"回覆訊息時發生未預期錯誤: {exc}")
        return False


def process_message_in_background(event):
    """在背景處理耗時的訊息回覆"""
    user_id = event.source.user_id

    try:
        reply_message_obj = main_reply_message(event)
    except Exception as e:
        logger.exception(f"處理訊息時發生錯誤: {e}")
        reply_message_obj = TextMessage(
            text="抱歉，系統發生錯誤，請稍後再試。"
        )

    if reply_message_obj:
        if not send_reply_with_fallback(event, reply_message_obj, user_id):
            logger.error(
                "回覆訊息與推播皆失敗，使用者 %s，訊息內容型別 %s",
                user_id,
                type(reply_message_obj).__name__,
            )
