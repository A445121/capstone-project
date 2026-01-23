import logging
import os
import sys
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("app.log"),
    ],
)
logger = logging.getLogger(__name__)


class Config:
    """應用程式配置，集中管理所有環境變數"""
    DEBUG = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    PORT = int(os.getenv("PORT", 443))
    LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
    DB_SERVER = os.getenv("DB_SERVER", "localhost")
    DB_NAME = os.getenv("DB_NAME", "Project")
    DB_ODBC_DRIVER = os.getenv("DB_ODBC_DRIVER", "ODBC Driver 17 for SQL Server")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    VALIDATION_MODE = os.getenv("VALIDATION_MODE", "strict")

    @classmethod
    def validate(cls, exit_on_failure=None):
        """
        驗證必需的環境變數是否存在
        參數:
            exit_on_failure:
                - 如果為 True，驗證失敗時會終止程序
                - 如果為 False，驗證失敗時只會拋出例外
                - 如果為 None，則根據 VALIDATION_MODE 環境變數決定行為
        """
        missing_vars = []
        if not cls.LINE_CHANNEL_ACCESS_TOKEN:
            missing_vars.append("LINE_CHANNEL_ACCESS_TOKEN")
        if not cls.LINE_CHANNEL_SECRET:
            missing_vars.append("LINE_CHANNEL_SECRET")
        if not cls.DB_SERVER:
            missing_vars.append("DB_SERVER")
        if not cls.DB_NAME:
            missing_vars.append("DB_NAME")
        if missing_vars:
            error_msg = f"缺少以下必要環境變數: {', '.join(missing_vars)}"
            logger.error(error_msg)
            should_exit = (
                exit_on_failure
                if exit_on_failure is not None
                else cls.VALIDATION_MODE.lower() == "strict"
            )
            if should_exit:
                logger.critical("驗證失敗，程序將終止")
                sys.exit(1)
            raise ValueError(error_msg)
        return True


is_testing = os.environ.get("TESTING", "False").lower() == "true"
if not is_testing:
    try:
        Config.validate()
        logger.info("環境變數驗證成功")
    except ValueError as e:
        logger.error(f"環境變數驗證失敗: {e}")
else:
    logger.debug("TESTING 模式啟用，略過環境變數驗證")
