from flask import Blueprint

main_bp = Blueprint("main", __name__)
alarm_bp = Blueprint("alarm", __name__)

from . import main, alarm  # noqa: E402,F401


def register_blueprints(app):
    """註冊所有藍圖（Blueprint）到 Flask 應用程式。"""
    app.register_blueprint(main_bp)
    app.register_blueprint(alarm_bp)
