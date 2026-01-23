import html
import logging
import re
import sys

from linebot.v3.messaging import TextMessage

from src.database import db
from src.reply import dispatch_command

sys.stdout.reconfigure(encoding="utf-8")

_DISALLOWED_CHARS = re.compile(r"[^\w\s.,;?!@#$%^&*()\-=+\[\]{}:\"'/\\<>]")


def sanitize_input(text):
    """轉義 HTML，並移除不在白名單中的字元。"""
    if not isinstance(text, str):
        return ""
    escaped = html.escape(text, quote=False)
    return _DISALLOWED_CHARS.sub("", escaped)


def reply_message(event):
    """處理使用者訊息，並以規則式邏輯回傳回應。"""
    user_message = sanitize_input(event.message.text)
    user_id = event.source.user_id

    response = dispatch_command(user_message, db, user_id)
    
    if response:
        return response
    
    return TextMessage(text="抱歉，我不明白您的指令。請輸入「選單」或「Help」查看可用功能。")
