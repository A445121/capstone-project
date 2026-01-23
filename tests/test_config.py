import os
from unittest.mock import patch

os.environ["TESTING"] = "True"

from src.config import Config


def test_config_default_values():
    """測試未設定環境變數時，Config 是否載入預設值。"""
    mock_env = {
        "FLASK_DEBUG": "False",
        "PORT": "5000",
        "DB_SERVER": "localhost",
        "DB_NAME": "conversations"
    }
    with patch.dict(os.environ, mock_env, clear=True):
        assert Config.DEBUG is False
        assert Config.PORT == 443
        assert Config.DB_SERVER == "localhost"
        assert Config.DB_NAME == "Project"


def test_config_env_override(monkeypatch):
    """測試環境變數覆寫設定是否正確。"""
    monkeypatch.setenv("FLASK_DEBUG", "True")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("DB_SERVER", "my_db_server")
    monkeypatch.setenv("DB_NAME", "my_db_name")

    assert os.getenv("FLASK_DEBUG").lower() == "true"
    assert int(os.getenv("PORT")) == 8080
    assert os.getenv("DB_SERVER") == "my_db_server"
    assert os.getenv("DB_NAME") == "my_db_name"
