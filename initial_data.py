import logging
import os
import pathlib
from typing import Any

# 設定 pd 為 Any 類型，避免編輯器誤判 None 屬性錯誤
pd: Any = None
try:
    import pandas as pd
except ModuleNotFoundError:
    pass
import pyodbc

# 嘗試匯入資料庫模組（支援不同目錄結構）
try:
    from .database import db, Database  # type: ignore
except Exception:  # pragma: no cover
    from database import db, Database  # type: ignore

if db is None:
    db = Database()


# 1. 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 2. 設定 Excel 檔案路徑
# 預設在目前資料夾或 data 子資料夾尋找 Excel 檔案
THIS_DIR = pathlib.Path(__file__).resolve().parent

# 也可以透過環境變數指定路徑
excel_env = os.getenv("EXCEL_FILE_PATH")
if excel_env:
    EXCEL_FILE_PATH = pathlib.Path(excel_env).expanduser()
else:
    candidate0 = THIS_DIR / "simulated_data (1).xlsx"
    candidate1 = THIS_DIR / "data" / "simulated_data (1).xlsx"
    candidate2 = THIS_DIR.parent / "data" / "simulated_data (1).xlsx"
    if candidate0.exists():
        EXCEL_FILE_PATH = candidate0
    elif candidate1.exists():
        EXCEL_FILE_PATH = candidate1
    else:
        EXCEL_FILE_PATH = candidate2


def _parse_excel_datetime(value):
    """將 Excel 儲存格轉為 datetime，失敗回傳 None。"""
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def _get_total_operation_sec(row):
    """取得 total_operation_sec，若只有 total_operation_hrs 則換算成秒。"""
    if pd.notna(row.get("total_operation_sec")):
        return int(row.get("total_operation_sec"))
    if pd.notna(row.get("total_operation_hrs")):
        return int(float(row.get("total_operation_hrs")) * 3600)
    return None


# 3. 資料匯入設定
# 定義 Excel 工作表與資料庫資料表的對應關係

TABLE_CONFIGS = [
    {
        "excel_sheet_name": "equipment",
        "sql_table_name": "equipment",
        "sql_columns": ["equipment_id", "name", "equipment_type", "location", "status", "last_updated"],
        "transform_row_data": lambda row: (
            row.get('equipment_id'),
            row.get('name'),
            row.get('equipment_type'),
            row.get('location'),
            row.get('status'),
            pd.to_datetime(row.get('last_updated'))
            if pd.notna(row.get('last_updated'))
            else None,
        )
    },
    {
    "excel_sheet_name": "alert_history",
    "sql_table_name": "alert_history",
    "sql_columns": [
        "error_id",
        "equipment_id",
        "detected_anomaly_type",
        "severity",
        "is_resolved",
        "created_time",
        "resolved_time",
        "resolved_by",
        "resolution_notes",
    ],
    "transform_row_data": lambda row: (
        row.get("error_id"),
        row.get("equipment_id"),
        row.get("detected_anomaly_type"),
        (
            row.get("severity")
            if pd.notna(row.get("severity"))
            else (
                row.get("severity_level")
                if pd.notna(row.get("severity_level"))
                else None
            )
        ),
        row.get("is_resolved"),
        pd.to_datetime(row.get("created_time"))
        if pd.notna(row.get("created_time")) else None,
        pd.to_datetime(row.get("resolved_time"))
        if pd.notna(row.get("resolved_time")) else None,
        str(row.get("resolved_by"))
        if pd.notna(row.get("resolved_by")) else None,
        str(row.get("resolution_notes"))
        if pd.notna(row.get("resolution_notes")) else None,
    ),
},

    {
        "excel_sheet_name": "equipment_metrics",
        "sql_table_name": "equipment_metrics",
        "sql_columns": ["id", "equipment_id", "metric_type", "status",
                        "value", "threshold_min", "threshold_max", "unit", "last_updated"],
        "transform_row_data": lambda row: (
            row.get('id'),
            row.get('equipment_id'),
            row.get('metric_type'),
            str(row.get('status')) if pd.notna(row.get('status')) else None,
            row.get('value'),
            row.get('threshold_min'),
            row.get('threshold_max'),
            str(row.get('unit')) if pd.notna(row.get('unit')) else None,
            pd.to_datetime(row.get('last_updated'))
            if pd.notna(row.get('last_updated')) else None,
        )
    },
    {
        "excel_sheet_name": "equipment_metric_thresholds",
        "sql_table_name": "equipment_metric_thresholds",
        "sql_columns": ["metric_type", "normal_value", "warning_min", "warning_max",
                        "critical_min", "critical_max", "emergency_op", "emergency_min",
                        "emergency_max", "last_updated"],
        "transform_row_data": lambda row: (
            str(row.get('metric_type'))
            if row.get('metric_type') else 'default_metric_type',
            float(row.get('normal_value'))
            if pd.notna(row.get('normal_value')) else None,
            float(row.get('warning_min'))
            if pd.notna(row.get('warning_min')) else None,
            float(row.get('warning_max'))
            if pd.notna(row.get('warning_max')) else None,
            float(row.get('critical_min'))
            if pd.notna(row.get('critical_min')) else None,
            float(row.get('critical_max'))
            if pd.notna(row.get('critical_max')) else None,
            str(row.get('emergency_op'))
            if pd.notna(row.get('emergency_op')) else None,
            float(row.get('emergency_min'))
            if pd.notna(row.get('emergency_min')) else None,
            float(row.get('emergency_max'))
            if pd.notna(row.get('emergency_max')) else None,
            pd.to_datetime(row.get('last_updated'))
            if pd.notna(row.get('last_updated')) else None,
        )
    },
    {
    "excel_sheet_name": "error_logs",
    "sql_table_name": "error_logs",
    "sql_columns": [
        "log_date",
        "error_id",
        "equipment_id",
        "deformation_mm",
        "rpm",
        "event_time",
        "detected_anomaly_type",
        "downtime_sec",
        "resolved_time",
        "severity",
    ],
    "transform_row_data": lambda row: (
        pd.to_datetime(row.get("log_date"))
        if pd.notna(row.get("log_date")) else None,
        int(row.get("error_id")) if pd.notna(row.get("error_id")) else None,
        str(row.get("equipment_id"))
        if pd.notna(row.get("equipment_id")) else None,
        float(row.get("deformation_mm"))
        if pd.notna(row.get("deformation_mm"))
        else (
            float(row.get("deformation(mm)"))
            if pd.notna(row.get("deformation(mm)")) else 0.0
        ),
        int(row.get("rpm")) if pd.notna(row.get("rpm")) else None,
        pd.to_datetime(row.get("event_time"))
        if pd.notna(row.get("event_time")) else None,
        str(row.get("detected_anomaly_type"))
        if pd.notna(row.get("detected_anomaly_type"))
        else None,
        int(row.get("downtime_sec")) if pd.notna(row.get("downtime_sec")) else None,
        pd.to_datetime(row.get("resolved_time"))
        if pd.notna(row.get("resolved_time")) else None,
        (
            str(row.get("severity"))
            if pd.notna(row.get("severity"))
            else (
                str(row.get("severity_level"))
                if pd.notna(row.get("severity_level")) else None
            )
        ),
    ),
},

    {
        "excel_sheet_name": "stats_operational_monthly",
        "sql_table_name": "stats_operational_monthly",
        "sql_columns": ["equipment_id", "year", "month",
                        "total_operation_sec", "downtime_sec",
                        "downtime_rate_percent"],
        "transform_row_data": lambda row: (
            str(row.get('equipment_id')),
            int(row.get('year')),
            int(row.get('month')),
            _get_total_operation_sec(row),
            int(row.get('downtime_sec'))
            if pd.notna(row.get('downtime_sec')) else None,
            str(row.get('downtime_rate_percent'))
            if pd.notna(row.get('downtime_rate_percent')) else None,
        )
    },
    {
        "excel_sheet_name": "stats_operational_quarterly",
        "sql_table_name": "stats_operational_quarterly",
        "sql_columns": ["equipment_id", "year", "quarter",
                        "total_operation_sec", "downtime_sec",
                        "downtime_rate_percent"],
        "transform_row_data": lambda row: (
            str(row.get('equipment_id')),
            row.get('year'),
            row.get('quarter'),
            _get_total_operation_sec(row),
            int(row.get('downtime_sec'))
            if pd.notna(row.get('downtime_sec')) else None,
            str(row.get('downtime_rate_percent'))
            if pd.notna(row.get('downtime_rate_percent')) else None,
        )
    },
    {
        "excel_sheet_name": "stats_operational_yearly",
        "sql_table_name": "stats_operational_yearly",
        "sql_columns": ["equipment_id", "year", "total_operation_sec",
                        "downtime_sec", "downtime_rate_percent"],
        "transform_row_data": lambda row: (
            str(row.get('equipment_id')),
            row.get('year'),
            _get_total_operation_sec(row),
            int(row.get('downtime_sec'))
            if pd.notna(row.get('downtime_sec')) else None,
            str(row.get('downtime_rate_percent'))
            if pd.notna(row.get('downtime_rate_percent')) else None,
        )
    },
    {
        "excel_sheet_name": "stats_abnormal_monthly",
        "sql_table_name": "stats_abnormal_monthly",
        "sql_columns": ["equipment_id", "year", "month",
                        "detected_anomaly_type", "total_operation_sec", "downtime_sec",
                        "downtime_rate_percent"],
        "transform_row_data": lambda row: (
            str(row.get('equipment_id')),
            int(row.get('year')),
            int(row.get('month')),
            str(row.get('detected_anomaly_type'))
            if row.get('detected_anomaly_type') else 'default_anomaly_type',
            _get_total_operation_sec(row),
            int(row.get('downtime_sec'))
            if pd.notna(row.get('downtime_sec')) else None,
            str(row.get('downtime_rate_percent'))
            if pd.notna(row.get('downtime_rate_percent')) else None,
        )
    },
    {
        "excel_sheet_name": "stats_abnormal_quarterly",
        "sql_table_name": "stats_abnormal_quarterly",
        "sql_columns": ["equipment_id", "year", "quarter",
                        "detected_anomaly_type", "total_operation_sec", "downtime_sec",
                        "downtime_rate_percent"],
        "transform_row_data": lambda row: (
            str(row.get('equipment_id')),
            row.get('year'),
            row.get('quarter'),
            str(row.get('detected_anomaly_type'))
            if row.get('detected_anomaly_type') else 'default_anomaly_type',
            _get_total_operation_sec(row),
            int(row.get('downtime_sec'))
            if pd.notna(row.get('downtime_sec')) else None,
            str(row.get('downtime_rate_percent'))
            if pd.notna(row.get('downtime_rate_percent')) else None,
        )
    },
    {
        "excel_sheet_name": "stats_abnormal_yearly",
        "sql_table_name": "stats_abnormal_yearly",
        "sql_columns": ["equipment_id", "year", "detected_anomaly_type",
                        "total_operation_sec", "downtime_sec", "downtime_rate_percent"],
        "transform_row_data": lambda row: (
            str(row.get('equipment_id')),
            row.get('year'),
            str(row.get('detected_anomaly_type'))
            if row.get('detected_anomaly_type') else 'default_anomaly_type',
            _get_total_operation_sec(row),
            int(row.get('downtime_sec'))
            if pd.notna(row.get('downtime_sec')) else None,
            str(row.get('downtime_rate_percent'))
            if pd.notna(row.get('downtime_rate_percent')) else None,
        )
    },
    {
        "excel_sheet_name": "user_preferences",
        "sql_table_name": "user_preferences",
        "sql_columns": [
            "user_id",
            "language",
            "role",
            "is_admin",
            "responsible_area",
            "created_at",
            "display_name",
            "last_active",
        ],
        "transform_row_data": lambda row: (
            str(row.get('user_id')),
            str(row.get('language'))
            if pd.notna(row.get('language')) else None,
            str(row.get('role')) if pd.notna(row.get('role')) else None,
            int(row.get('is_admin')) if pd.notna(row.get('is_admin')) else None,
            str(row.get('responsible_area'))
            if pd.notna(row.get('responsible_area')) else None,
            _parse_excel_datetime(row.get('created_at')),
            str(row.get('display_name'))
            if pd.notna(row.get('display_name')) else None,
            _parse_excel_datetime(row.get('last_active')),
        )
    }
]


# 4. 執行匯入
def import_data_from_excel():
    """讀取 Excel 檔案並將資料匯入資料庫（若資料表已有資料則跳過）。"""
    if pd is None:
        logger.error("缺少 pandas，請先安裝：pip install pandas openpyxl")
        return
    if not EXCEL_FILE_PATH.exists():
        logger.error("找不到 Excel 檔案：%s", EXCEL_FILE_PATH)
        return
    try:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.fast_executemany = True
            logger.info("已啟用快速寫入模式。")

            for config in TABLE_CONFIGS:
                sheet_name = config["excel_sheet_name"]
                sql_table_name = config["sql_table_name"]
                sql_columns = config["sql_columns"]
                transform_row_data = config["transform_row_data"]

                logger.info(
                    "開始匯入資料表 %s（工作表：%s）。",
                    sql_table_name,
                    sheet_name,
                )

                try:
                    cursor.execute(f"SELECT COUNT(*) FROM [{sql_table_name}]")
                    if cursor.fetchone()[0] > 0:
                        logger.info(
                            "資料表 %s 已有資料，跳過匯入。",
                            sql_table_name,
                        )
                        continue

                    data_frame = pd.read_excel(
                        EXCEL_FILE_PATH, sheet_name=sheet_name
                    )
                    data_frame = data_frame.where(pd.notna(data_frame), None)

                    if data_frame.empty:
                        logger.warning("工作表 %s 沒有資料，跳過。", sheet_name)
                        continue

                    logger.info(
                        "讀取工作表 %s，共 %s 筆。",
                        sheet_name,
                        len(data_frame),
                    )

                    sql_columns_str = ", ".join(
                        [f"[{col}]" for col in sql_columns]
                    )
                    placeholders_str = ", ".join(["?" for _ in sql_columns])
                    insert_sql = (
                        f"INSERT INTO [{sql_table_name}] ({sql_columns_str}) "
                        f"VALUES ({placeholders_str})"
                    )

                    data_to_insert = [
                        transform_row_data(row) for _, row in data_frame.iterrows()
                    ]

                    if data_to_insert:
                        logger.info(
                            "準備寫入 %s 筆到資料表 %s。",
                            len(data_to_insert),
                            sql_table_name,
                        )
                        try:
                            # 執行批次寫入
                            cursor.executemany(insert_sql, data_to_insert)
                            conn.commit()
                            logger.info("資料表 %s 匯入完成。", sql_table_name)
                        except pyodbc.Error as e:
                            logger.error(
                                "寫入資料表 %s 失敗：%s",
                                sql_table_name,
                                e,
                            )
                            conn.rollback()

                except Exception as e:
                    logger.error(
                        "處理工作表 %s 時發生錯誤：%s",
                        sheet_name,
                        e,
                    )
                    continue

    except Exception as e:
        logger.error("匯入流程發生錯誤：%s", e)


if __name__ == "__main__":
    # 當直接執行此檔案時，開始匯入資料
    logger.info("開始匯入 Excel 資料。")
    import_data_from_excel()
    logger.info("匯入流程結束。")
