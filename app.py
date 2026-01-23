import logging
import os
import sys
import secrets
from flask import Flask
from flask_talisman import Talisman
from werkzeug.middleware.proxy_fix import ProxyFix

from src.config import Config
from src.initial_data import import_data_from_excel
from src.routes import register_blueprints
from src.linebot_connect import get_handler
from src.routes.main import set_handler

logger = logging.getLogger(__name__)


def create_app(testing=False):
    """建立並設定 Flask 應用程式"""
    try:
        try:
            Config.validate()
            logger.info("環境變數驗證成功")

        except ValueError as e:
            if not testing:
                logger.critical("環境變數驗證失敗，程式將終止: %s", e)
                sys.exit(1)
            else:
                logger.warning(f"測試模式: 環境變數驗證失敗，但將繼續執行: {e}")

        app = Flask(__name__)

        app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(24)

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

        if not testing:
            csp = {
                "default-src": "'self'",
                "script-src": [
                    "'self'"
                ],
                "style-src": ["'self'"],
                "img-src": "'self'"
            }
            Talisman(
                app,
                content_security_policy=csp,
                content_security_policy_nonce_in=["script-src"],
                force_https=True,
                session_cookie_secure=True,
                session_cookie_http_only=True,
                feature_policy="geolocation 'none'; microphone 'none'; camera 'none'",
            )

        import_data_from_excel()

        register_blueprints(app)

        handler = get_handler()
        set_handler(handler)

        return app
    except Exception as e:
        logger.critical(f"應用程式初始化失敗: {e}")
        raise


def run_app(host=None, port=None, debug=None, ssl_context=None):
    """執行 Flask 應用程式"""
    host = host or os.environ.get("HOST", "0.0.0.0")
    port = port or int(os.environ.get("PORT", 443))
    debug = debug or (os.environ.get("FLASK_DEBUG", "False").lower() == "true")
    try:
        ssl_context = (
            os.environ.get('SSL_CERT_PATH', 'chain.pem'),
            os.environ.get('SSL_KEY_PATH', 'key.pem')
        )
    except:
        pass
    logger.info("啟動應用程式，反向代理模式: %s", os.environ.get('Reverse_Proxy', 'true'))
    logger.info("主機: %s, 埠: %d, 除錯模式: %s", host, port, debug)
    logger.info("SSL 憑證: %s, SSL 密鑰: %s", ssl_context[0] if ssl_context else None, ssl_context[1] if ssl_context else None)
    if os.environ.get('Reverse_Proxy')=='true':
        logger.info("在反向代理後執行，停用 SSL")
        app = create_app()
        app.run(host=host, port=port, debug=debug)
    else:
        app = create_app()
        app.run(host=host, port=port, debug=debug, ssl_context=ssl_context)


if __name__ == "__main__":
    run_app()
