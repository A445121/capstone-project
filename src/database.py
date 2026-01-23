import contextlib
import datetime
import logging
import os
import sys
import threading
from queue import Empty, Full, Queue

import pyodbc

if __package__ is None or __package__ == "":
    import pathlib

    sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
    __package__ = "src"

from .config import Config  # noqa: E402


logger = logging.getLogger(__name__)


class SimpleConnectionPool:
    """簡易且具執行緒安全的 PyODBC 連線池。"""

    def __init__(self, connect_func, max_size=10, timeout=5):
        self.connect_func = connect_func
        self.max_size = max_size
        self.timeout = timeout
        self.pool = Queue(maxsize=max_size)
        self._created_count = 0
        self._lock = threading.Lock()

    def get_connection(self):
        """從連線池取得連線，必要時建立新連線。"""
        try:
            return self.pool.get(block=False)
        except Empty:
            with self._lock:
                if self._created_count < self.max_size:
                    try:
                        conn = self.connect_func()
                        self._created_count += 1
                        return conn
                    except Exception as e:
                        logger.error(
                            "Failed to create new DB connection: %s",
                            e,
                        )
                        raise

            try:
                return self.pool.get(timeout=self.timeout)
            except Empty:
                logger.error("Database connection pool exhausted.")
                raise Exception("Database connection pool exhausted")

    def return_connection(self, conn):
        """將連線放回池中，並回滾尚未完成的交易。"""
        if conn is None:
            return

        try:
            conn.rollback()
            self.pool.put(conn, block=False)
        except (Full, pyodbc.Error, Exception):
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created_count -= 1

    def close_all(self):
        """關閉連線池中的所有連線。"""
        while not self.pool.empty():
            try:
                conn = self.pool.get(block=False)
                conn.close()
            except (Empty, Exception):
                pass
        with self._lock:
            self._created_count = 0


class Database:
    """處理對話記錄與使用者偏好儲存的資料庫處理程序"""

    def __init__(self, server=None, database=None, user=None, password=None):
        """初始化資料庫連線"""
        resolved_server = server if server is not None else Config.DB_SERVER
        resolved_database = (
            database if database is not None else Config.DB_NAME
        )
        resolved_user = user if user is not None else Config.DB_USER
        resolved_password = (
            password if password is not None else Config.DB_PASSWORD
        )

        logger.info("初始化資料庫連線字串...")
        windows_login = (os.getenv("Windows_login") or "").lower()
        logger.info(
            "使用的伺服器: %s, 伺服器: %s, 資料庫: %s",
            windows_login,
            resolved_server,
            resolved_database,
        )

        if not bool(os.getenv("Windows_login")):
            self.connection_string = (
                f"DRIVER={{{Config.DB_ODBC_DRIVER}}};"
                f"SERVER={resolved_server};"
                f"DATABASE={resolved_database};"
                f"UID={resolved_user};"
                f"PWD={resolved_password};"
                "Trusted_Connection=no;"
                "Encrypt=no;"
                "TrustServerCertificate=yes;"
            )
        else:
            self.connection_string = (
                "DRIVER={ODBC Driver 17 for SQL Server};"
                f"SERVER={resolved_server};"
                f"DATABASE={resolved_database};"
                "Trusted_Connection=yes;"
            )

        self.pool = SimpleConnectionPool(
            self._create_new_connection,
            max_size=10,
        )
        self._initialize_db()

    def _create_new_connection(self):
        """建立一個新的資料庫連線（供連線池使用）"""
        return pyodbc.connect(self.connection_string)

    @contextlib.contextmanager
    def _get_connection(self):
        """
        取得資料庫連線的內容管理器（使用連線池）。
        用法:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ...
        """
        conn = self.pool.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.return_connection(conn)

    def _initialize_db(self):
        """
        如果資料表尚未存在，則建立必要的表格。
        此版本已加上主鍵與外鍵約束以確保資料完整性。
        """
        try:
            with self._get_connection() as conn:
                init_cur = conn.cursor()

                equipment_cols = """
                    [id] INT NULL,
                    [equipment_id] NVARCHAR(255) NOT NULL PRIMARY KEY,
                    [name] NVARCHAR(255) NOT NULL,
                    [equipment_type] NVARCHAR(255) NULL,
                    [location] NVARCHAR(255) NULL,
                    [status] NVARCHAR(255) NOT NULL,
                    [last_updated] datetime2(2) NULL
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "equipment",
                    equipment_cols,
                )

                user_equipment_subscriptions_cols = """
                    [subscription_id] INT IDENTITY(1,1) PRIMARY KEY,
                    [user_id] NVARCHAR(255) NOT NULL,
                    [notification_level] NVARCHAR(50) NULL,
                    [subscribed_at] datetime2(2) NULL DEFAULT GETDATE(),
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    CONSTRAINT FK_subscriptions_equipment
                        FOREIGN KEY (equipment_id)
                        REFERENCES equipment(equipment_id),
                    CONSTRAINT UQ_user_equipment UNIQUE(user_id, equipment_id)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "user_equipment_subscriptions",
                    user_equipment_subscriptions_cols,
                )

                self._create_index_if_not_exists(
                    init_cur,
                    "idx_subscriptions_equipment",
                    "user_equipment_subscriptions",
                    ["equipment_id"],
                )

                alert_history_cols = """
                    [error_id] INT NOT NULL PRIMARY KEY,
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [detected_anomaly_type] NVARCHAR(255) NULL,
                    [severity_level] NVARCHAR(255) NULL,
                    [is_resolved] BIT NULL DEFAULT 0,
                    [created_time] datetime2(2) NULL,
                    [resolved_time] datetime2(2) NULL,
                    [resolved_by] NVARCHAR(255) NULL,
                    [resolution_notes] NVARCHAR(MAX) NULL
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "alert_history",
                    alert_history_cols,
                )

                equipment_metrics_cols = """
                    [id] INT NOT NULL PRIMARY KEY,
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [metric_type] NVARCHAR(255) NOT NULL,
                    [status] NVARCHAR(50) NULL,
                    [value] FLOAT NULL,
                    [threshold_min] FLOAT NULL,
                    [threshold_max] FLOAT NULL,
                    [unit] NVARCHAR(50) NULL,
                    [last_updated] datetime2(2) NULL DEFAULT GETDATE()
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "equipment_metrics",
                    equipment_metrics_cols,
                )

                equipment_metric_thresholds_cols = """
                    [metric_type] NVARCHAR(50) NOT NULL PRIMARY KEY,
                    [normal_value] FLOAT NULL,
                    [warning_min] FLOAT NULL,
                    [warning_max] FLOAT NULL,
                    [critical_min] FLOAT NULL,
                    [critical_max] FLOAT NULL,
                    [emergency_op] NVARCHAR(10) NULL,
                    [emergency_min] FLOAT NULL,
                    [emergency_max] FLOAT NULL,
                    [last_updated] datetime2(2) NULL DEFAULT GETDATE()
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "equipment_metric_thresholds",
                    equipment_metric_thresholds_cols,
                )

                error_logs_cols = """
                    [log_date] DATE NOT NULL,
                    [error_id] INT NOT NULL PRIMARY KEY,
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [deformation_mm] FLOAT NOT NULL,
                    [rpm] INT NOT NULL,
                    [event_time] datetime2(2) NOT NULL,
                    [detected_anomaly_type] NVARCHAR(MAX) NOT NULL,
                    [downtime_sec] INT NULL,
                    [resolved_time] datetime2(2) NULL,
                    [severity_level] NVARCHAR(MAX) NULL
                """
                self._create_table_if_not_exists(
                    init_cur, "error_logs", error_logs_cols
                )

                stats_abnormal_monthly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [month] INT NOT NULL,
                    [detected_anomaly_type] NVARCHAR(255) NOT NULL,
                    [total_operation_hrs] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    [notes] NVARCHAR(MAX) NULL,
                    PRIMARY KEY (
                        equipment_id, year, month, detected_anomaly_type
                    )
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_abnormal_monthly",
                    stats_abnormal_monthly_cols,
                )

                stats_abnormal_quarterly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [quarter] INT NOT NULL,
                    [detected_anomaly_type] NVARCHAR(255) NOT NULL,
                    [total_operation_hrs] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    [notes] NVARCHAR(MAX) NULL,
                    PRIMARY KEY (
                        equipment_id, year, quarter, detected_anomaly_type
                    )
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_abnormal_quarterly",
                    stats_abnormal_quarterly_cols,
                )

                stats_abnormal_yearly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [detected_anomaly_type] NVARCHAR(255) NOT NULL,
                    [total_operation_hrs] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    [notes] NVARCHAR(MAX) NULL,
                    PRIMARY KEY (equipment_id, year, detected_anomaly_type)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_abnormal_yearly",
                    stats_abnormal_yearly_cols,
                )

                stats_operational_monthly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [month] INT NOT NULL,
                    [total_operation_hrs] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    [notes] NVARCHAR(MAX) NULL,
                    PRIMARY KEY (equipment_id, year, month)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_operational_monthly",
                    stats_operational_monthly_cols,
                )

                stats_operational_quarterly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [quarter] INT NOT NULL,
                    [total_operation_hrs] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    [notes] NVARCHAR(MAX) NULL,
                    PRIMARY KEY (equipment_id, year, quarter)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_operational_quarterly",
                    stats_operational_quarterly_cols,
                )

                stats_operational_yearly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL,
                    [year] INT NOT NULL,
                    [total_operation_hrs] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    [notes] NVARCHAR(MAX) NULL,
                    PRIMARY KEY (equipment_id, year),
                    CONSTRAINT FK_stats_op_yearly_equip
                        FOREIGN KEY (equipment_id)
                        REFERENCES equipment(equipment_id)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_operational_yearly",
                    stats_operational_yearly_cols,
                )

                logger.info("資料庫表格初始化/檢查完成 (已建立主鍵與外鍵約束)。")

        except pyodbc.Error as e:
            logger.exception(f"資料庫初始化期間發生 pyodbc 錯誤: {e}")
            raise
        except Exception as ex:
            logger.exception(f"資料庫初始化期間發生非預期錯誤: {ex}")
            raise

    def _create_table_if_not_exists(
        self,
        cursor,
        table_name,
        columns_definition,
    ):
        """通用方法，用於檢查並建立資料表"""
        check_table_sql = (
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ?;"
        )
        cursor.execute(check_table_sql, (table_name,))
        if cursor.fetchone()[0] == 0:
            create_table_sql = (
                f"CREATE TABLE {table_name} ({columns_definition});"
            )
            cursor.execute(create_table_sql)
            logger.info(f"資料表 '{table_name}' 已建立。")
        else:
            logger.info(f"資料表 '{table_name}' 已存在，跳過建立。")

    def _create_index_if_not_exists(
        self, cursor, index_name, table_name, columns
    ):
        """
        若索引不存在則建立索引。
        columns：要包含在索引中的欄位名稱清單。
        """
        check_index_sql = (
            "SELECT COUNT(*) FROM sys.indexes "
            "WHERE name = ? AND object_id = OBJECT_ID(?);"
        )
        cursor.execute(check_index_sql, (index_name, table_name))
        if cursor.fetchone()[0] == 0:
            cols_str = ", ".join(columns)
            create_index_sql = (
                f"CREATE INDEX {index_name} ON {table_name} ({cols_str});"
            )
            cursor.execute(create_index_sql)
            logger.info(f"索引 '{index_name}' 於 '{table_name}' 已建立。")
        else:
            logger.info(f"索引 '{index_name}' 於 '{table_name}' 已存在，跳過建立。")

    @staticmethod
    def _count_workdays(start_date: datetime.date, end_date: datetime.date) -> int:
        """計算週一到週六的工作日天數。"""
        count = 0
        current = start_date
        while current <= end_date:
            if current.weekday() < 6:
                count += 1
            current += datetime.timedelta(days=1)
        return count

    @staticmethod
    def _month_range(year: int, month: int) -> tuple:
        start = datetime.date(year, month, 1)
        if month == 12:
            end = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            end = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        return start, end

    def _calc_month_operation_hours(self, year: int, month: int) -> int:
        start, end = self._month_range(year, month)
        workdays = self._count_workdays(start, end)
        return workdays * 12

    def _calc_quarter_operation_hours(self, year: int, quarter: int) -> int:
        start_month = (quarter - 1) * 3 + 1
        return sum(
            self._calc_month_operation_hours(year, month)
            for month in range(start_month, start_month + 3)
        )

    def _calc_year_operation_hours(self, year: int) -> int:
        return sum(
            self._calc_month_operation_hours(year, month)
            for month in range(1, 13)
        )

    @staticmethod
    def _format_downtime_rate(
        downtime_sec: int, total_operation_hrs: int
    ) -> str:
        if not total_operation_hrs:
            return "0%"
        rate = (downtime_sec / (total_operation_hrs * 3600)) * 100
        return f"{rate:.2f}%"

    def refresh_stats(self) -> None:
        """
        依據 error_logs 重新計算六張統計表。
        運作時數以「週一到週六、每日 08:00~20:00 (12 小時)」估算。
        """
        stats_tables = [
            "stats_abnormal_monthly",
            "stats_abnormal_quarterly",
            "stats_abnormal_yearly",
            "stats_operational_monthly",
            "stats_operational_quarterly",
            "stats_operational_yearly",
        ]

        with self._get_connection() as conn:
            cursor = conn.cursor()

            for table in stats_tables:
                cursor.execute(f"DELETE FROM {table};")

            cursor.execute("SELECT COUNT(*) FROM error_logs;")
            if cursor.fetchone()[0] == 0:
                logger.info("error_logs 無資料，統計表已清空。")
                return

            cursor.execute(
                """
                SELECT
                    equipment_id,
                    YEAR(event_time) AS year,
                    MONTH(event_time) AS month,
                    detected_anomaly_type,
                    SUM(COALESCE(downtime_sec,
                        DATEDIFF(second, event_time, resolved_time), 0)) AS downtime_sec
                FROM error_logs
                GROUP BY
                    equipment_id, YEAR(event_time), MONTH(event_time),
                    detected_anomaly_type;
                """
            )
            abnormal_month_rows = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    equipment_id,
                    YEAR(event_time) AS year,
                    MONTH(event_time) AS month,
                    SUM(COALESCE(downtime_sec,
                        DATEDIFF(second, event_time, resolved_time), 0)) AS downtime_sec
                FROM error_logs
                GROUP BY equipment_id, YEAR(event_time), MONTH(event_time);
                """
            )
            operational_month_rows = cursor.fetchall()

            month_hours_cache = {}
            quarter_hours_cache = {}
            year_hours_cache = {}

            def get_month_hours(year: int, month: int) -> int:
                key = (year, month)
                if key not in month_hours_cache:
                    month_hours_cache[key] = self._calc_month_operation_hours(
                        year, month
                    )
                return month_hours_cache[key]

            def get_quarter_hours(year: int, quarter: int) -> int:
                key = (year, quarter)
                if key not in quarter_hours_cache:
                    quarter_hours_cache[key] = (
                        self._calc_quarter_operation_hours(year, quarter)
                    )
                return quarter_hours_cache[key]

            def get_year_hours(year: int) -> int:
                if year not in year_hours_cache:
                    year_hours_cache[year] = self._calc_year_operation_hours(year)
                return year_hours_cache[year]

            abnormal_month_data = []
            abnormal_quarter_map = {}
            abnormal_year_map = {}

            for row in abnormal_month_rows:
                equipment_id, year, month, anomaly, downtime_sec = row
                anomaly = anomaly or "未知"
                downtime_sec = downtime_sec or 0
                total_hours = get_month_hours(year, month)
                rate = self._format_downtime_rate(downtime_sec, total_hours)
                abnormal_month_data.append(
                    (
                        equipment_id,
                        year,
                        month,
                        anomaly,
                        total_hours,
                        downtime_sec,
                        rate,
                        None,
                    )
                )

                quarter = (month - 1) // 3 + 1
                quarter_key = (equipment_id, year, quarter, anomaly)
                abnormal_quarter_map[quarter_key] = (
                    abnormal_quarter_map.get(quarter_key, 0) + downtime_sec
                )

                year_key = (equipment_id, year, anomaly)
                abnormal_year_map[year_key] = (
                    abnormal_year_map.get(year_key, 0) + downtime_sec
                )

            operational_month_data = []
            operational_quarter_map = {}
            operational_year_map = {}

            for row in operational_month_rows:
                equipment_id, year, month, downtime_sec = row
                downtime_sec = downtime_sec or 0
                total_hours = get_month_hours(year, month)
                rate = self._format_downtime_rate(downtime_sec, total_hours)
                operational_month_data.append(
                    (
                        equipment_id,
                        year,
                        month,
                        total_hours,
                        downtime_sec,
                        rate,
                        None,
                    )
                )

                quarter = (month - 1) // 3 + 1
                quarter_key = (equipment_id, year, quarter)
                operational_quarter_map[quarter_key] = (
                    operational_quarter_map.get(quarter_key, 0) + downtime_sec
                )

                year_key = (equipment_id, year)
                operational_year_map[year_key] = (
                    operational_year_map.get(year_key, 0) + downtime_sec
                )

            abnormal_quarter_data = []
            for (equipment_id, year, quarter, anomaly), downtime_sec in abnormal_quarter_map.items():
                total_hours = get_quarter_hours(year, quarter)
                rate = self._format_downtime_rate(downtime_sec, total_hours)
                abnormal_quarter_data.append(
                    (
                        equipment_id,
                        year,
                        quarter,
                        anomaly,
                        total_hours,
                        downtime_sec,
                        rate,
                        None,
                    )
                )

            abnormal_year_data = []
            for (equipment_id, year, anomaly), downtime_sec in abnormal_year_map.items():
                total_hours = get_year_hours(year)
                rate = self._format_downtime_rate(downtime_sec, total_hours)
                abnormal_year_data.append(
                    (
                        equipment_id,
                        year,
                        anomaly,
                        total_hours,
                        downtime_sec,
                        rate,
                        None,
                    )
                )

            operational_quarter_data = []
            for (equipment_id, year, quarter), downtime_sec in operational_quarter_map.items():
                total_hours = get_quarter_hours(year, quarter)
                rate = self._format_downtime_rate(downtime_sec, total_hours)
                operational_quarter_data.append(
                    (
                        equipment_id,
                        year,
                        quarter,
                        total_hours,
                        downtime_sec,
                        rate,
                        None,
                    )
                )

            operational_year_data = []
            for (equipment_id, year), downtime_sec in operational_year_map.items():
                total_hours = get_year_hours(year)
                rate = self._format_downtime_rate(downtime_sec, total_hours)
                operational_year_data.append(
                    (
                        equipment_id,
                        year,
                        total_hours,
                        downtime_sec,
                        rate,
                        None,
                    )
                )

            if abnormal_month_data:
                cursor.executemany(
                    """
                    INSERT INTO stats_abnormal_monthly (
                        equipment_id, year, month, detected_anomaly_type,
                        total_operation_hrs, downtime_sec, downtime_rate_percent, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    abnormal_month_data,
                )

            if abnormal_quarter_data:
                cursor.executemany(
                    """
                    INSERT INTO stats_abnormal_quarterly (
                        equipment_id, year, quarter, detected_anomaly_type,
                        total_operation_hrs, downtime_sec, downtime_rate_percent, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    abnormal_quarter_data,
                )

            if abnormal_year_data:
                cursor.executemany(
                    """
                    INSERT INTO stats_abnormal_yearly (
                        equipment_id, year, detected_anomaly_type,
                        total_operation_hrs, downtime_sec, downtime_rate_percent, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    abnormal_year_data,
                )

            if operational_month_data:
                cursor.executemany(
                    """
                    INSERT INTO stats_operational_monthly (
                        equipment_id, year, month,
                        total_operation_hrs, downtime_sec, downtime_rate_percent, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    operational_month_data,
                )

            if operational_quarter_data:
                cursor.executemany(
                    """
                    INSERT INTO stats_operational_quarterly (
                        equipment_id, year, quarter,
                        total_operation_hrs, downtime_sec, downtime_rate_percent, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    operational_quarter_data,
                )

            if operational_year_data:
                cursor.executemany(
                    """
                    INSERT INTO stats_operational_yearly (
                        equipment_id, year,
                        total_operation_hrs, downtime_sec, downtime_rate_percent, notes
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    operational_year_data,
                )

            logger.info("統計表更新完成。")

    def insert_alert_history(self, log_data: dict):
        """
        在單筆紀錄中，同時新增警報到 alert_history 和日誌到 error_logs。
        """
        sql_alert_history = """
            INSERT INTO alert_history (
                error_id, equipment_id, detected_anomaly_type,
                severity_level, created_time
            ) VALUES (?, ?, ?, ?, ?);
        """
        sql_error_log = """
            INSERT INTO error_logs (
                log_date, error_id, equipment_id, deformation_mm,
                rpm, event_time, detected_anomaly_type, severity_level
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        sql_get_max = "SELECT ISNULL(MAX(error_id), 0) FROM alert_history;"

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute(sql_get_max)
                latest_error_id = cursor.fetchone()[0] + 1
                event_time = datetime.datetime.now()

                cursor.execute(
                    sql_alert_history,
                    latest_error_id,
                    log_data["equipment_id"],
                    log_data["detected_anomaly_type"],
                    log_data["severity_level"],
                    event_time,
                )

                cursor.execute(
                    sql_error_log,
                    event_time.date(),
                    latest_error_id,
                    log_data["equipment_id"],
                    log_data.get("deformation_mm", 0),
                    log_data.get("rpm", 30000),
                    event_time,
                    log_data["detected_anomaly_type"],
                    log_data["severity_level"],
                )

                logger.info(
                    "成功寫入一筆異常紀錄，equipment_id: %s",
                    log_data["equipment_id"],
                )
                return {
                    "error_id": latest_error_id,
                    "created_time": event_time,
                }
        except pyodbc.Error as ex:
            logger.error(f"資料庫寫入時發生錯誤: {ex}")
            raise

    def get_alert_info(self, error_id: int, detected_anomaly_type: str):
        """用 error_id 跟 detected_anomaly_type 取得單筆警報的資訊"""
        sql = (
            "SELECT equipment_id, detected_anomaly_type FROM alert_history "
            "WHERE error_id = ? AND detected_anomaly_type =  ?;"
        )
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, error_id, detected_anomaly_type)
                row = cursor.fetchone()
                if row:
                    return {"equipment_id": row[0]}
                return None
        except pyodbc.Error as e:
            logger.error(
                "查詢警報資訊 (error_id: %s), detected_anomaly_type: %s) 失敗: %s",
                error_id,
                detected_anomaly_type,
                e,
            )
            return None

    def resolve_alert_history(self, log_data: dict):
        """
        將指定的警報紀錄更新為已解決狀態
        """
        sql_alert_history = """
        UPDATE alert_history
           SET is_resolved = 1,
               resolved_time = GETDATE(),
               resolved_by = ?,
               resolution_notes = ?
        OUTPUT inserted.resolved_time
         WHERE error_id = ? AND detected_anomaly_type = ? AND equipment_id = ?
           AND (is_resolved = 0);
        """
        sql_error_log = """
        UPDATE error_logs
           SET resolved_time = GETDATE(),
               downtime_sec = DATEDIFF(second, event_time, GETDATE())
         WHERE error_id = ?;
        """

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                notes = log_data.get("resolution_notes")
                if notes == "":
                    notes = None
                cursor.execute(
                    sql_alert_history,
                    log_data["resolved_by"],
                    notes,
                    log_data["error_id"],
                    log_data["detected_anomaly_type"],
                    log_data["equipment_id"],
                )

                newly_resolved_time = cursor.fetchone()
                if newly_resolved_time:
                    cursor.execute(sql_error_log, log_data["error_id"])
                    logger.info(
                        "成功將 error_id: %s / detected_anomaly_type: %s / "
                        "equipment_id: %s 的警報標示為已解決。",
                        log_data["error_id"],
                        log_data["detected_anomaly_type"],
                        log_data["equipment_id"],
                    )
                    return newly_resolved_time[0]

                check_sql = (
                    "SELECT resolved_time FROM alert_history "
                    "WHERE error_id = ? AND detected_anomaly_type = ? "
                    "AND equipment_id = ? AND is_resolved = 1;"
                )
                cursor.execute(
                    check_sql,
                    log_data["error_id"],
                    log_data["detected_anomaly_type"],
                    log_data["equipment_id"],
                )
                already_resolved_time = cursor.fetchone()

                if already_resolved_time:
                    logger.info(
                        "嘗試解決的 error_id: %s / equipment_id: %s / "
                        "detected_anomaly_type: %s 先前已被解決。",
                        log_data["error_id"],
                        log_data["equipment_id"],
                        log_data["detected_anomaly_type"],
                    )
                    return (already_resolved_time[0], "already_resolved")

                logger.warning(
                    "嘗試更新警報，但找不到對應的 error_id: %s / "
                    "detected_anomaly_type: %s。和equipment_id: %s。",
                    log_data["error_id"],
                    log_data["detected_anomaly_type"],
                    log_data["equipment_id"],
                )
                return None

        except pyodbc.Error as ex:
            error_id_val = log_data.get("error_id", "N/A")
            detected_anomaly_type_val = log_data.get(
                "detected_anomaly_type",
                "N/A",
            )
            equipment_id_val = log_data.get(
                "equipment_id",
                "N/A",
            )
            logger.error(
                "更新警報 (error_id: %s), detected_anomaly_type: %s, "
                "equipment_id: %s) 時發生資料庫錯誤: %s",
                error_id_val,
                detected_anomaly_type_val,
                equipment_id_val,
                ex,
            )
            raise

    def get_subscribed_users(self, equipment_id: str):
        """取得訂閱指定設備的所有使用者 ID"""
        sql = (
            "SELECT user_id FROM user_equipment_subscriptions "
            "WHERE equipment_id = ?;"
        )
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (equipment_id,))
                return [row[0] for row in cursor.fetchall()]
        except pyodbc.Error as e:
            logger.error(f"取得設備 {equipment_id} 訂閱者失敗: {e}")
            return []

    def get_user_subscriptions(self, user_id: str):
        """取得指定使用者訂閱的所有設備 ID"""
        sql = (
            "SELECT equipment_id FROM user_equipment_subscriptions "
            "WHERE user_id = ?;"
        )
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (user_id,))
                return [row[0] for row in cursor.fetchall()]
        except pyodbc.Error as e:
            logger.error(f"取得使用者 {user_id} 訂閱設備失敗: {e}")
            return []


if os.environ.get("TESTING", "False").lower() != "true":
    db = Database()
else:
    db = None
