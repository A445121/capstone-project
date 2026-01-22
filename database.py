import contextlib
import datetime
import logging
import os
import threading
from queue import Empty, Full, Queue
from typing import Optional

import pyodbc


def _is_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


class Config:
    DB_SERVER = os.getenv("DB_SERVER", os.getenv("MSSQL_SERVER", "localhost"))
    DB_NAME = os.getenv("DB_NAME", os.getenv("MSSQL_DATABASE", "master"))
    DB_USER = os.getenv("DB_USER", os.getenv("MSSQL_USER", "sa"))
    DB_PASSWORD = os.getenv("DB_PASSWORD", os.getenv("MSSQL_PASSWORD", ""))
    DB_ODBC_DRIVER = os.getenv(
        "DB_ODBC_DRIVER", "ODBC Driver 17 for SQL Server"
    )
    DB_TRUSTED_CONNECTION = os.getenv("DB_TRUSTED_CONNECTION", "")


logger = logging.getLogger(__name__)


class SimpleConnectionPool:
    """簡單的資料庫連線池，用於管理與重複使用連線。"""

    def __init__(self, connect_func, max_size=10, timeout=5):
        self.connect_func = connect_func
        self.max_size = max_size
        self.timeout = timeout
        self.pool = Queue(maxsize=max_size)
        self._created_count = 0
        self._lock = threading.Lock()

    def get_connection(self):
        """從連線池取得連線，若無可用連線則建立新的。"""
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

            # If pool is full, wait for a connection to be returned
            try:
                return self.pool.get(timeout=self.timeout)
            except Empty:
                logger.error("Database connection pool exhausted.")
                raise Exception("Database connection pool exhausted")

    def return_connection(self, conn):
        """將連線歸還給連線池。"""
        if conn is None:
            return

        try:
            # Ensure clean state for the next user
            conn.rollback()
            self.pool.put(conn, block=False)
        except (Full, pyodbc.Error, Exception):
            # If pool is full or connection is dead, close it
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created_count -= 1

    def close_all(self):
        """關閉連線池中所有的連線。"""
        while not self.pool.empty():
            try:
                conn = self.pool.get(block=False)
                conn.close()
            except (Empty, Exception):
                pass
        with self._lock:
            self._created_count = 0


class Database:
    """資料庫連線與資料表初始化。"""

    def __init__(self, server=None, database=None, user=None, password=None):
        """初始化資料庫設定，並確保資料表已建立。"""
        resolved_server = server if server is not None else Config.DB_SERVER
        resolved_database = (
            database if database is not None else Config.DB_NAME
        )
        resolved_user = user if user is not None else Config.DB_USER
        resolved_password = (
            password if password is not None else Config.DB_PASSWORD
        )

        logger.info("正在初始化資料庫連線設定...")
        trusted = (
            _is_truthy(os.getenv("Windows_login"))
            or _is_truthy(os.getenv("DB_TRUSTED_CONNECTION"))
            or _is_truthy(getattr(Config, "DB_TRUSTED_CONNECTION", None))
        )

        logger.info(
            "Trusted_Connection: %s, SERVER: %s, DATABASE: %s",
            trusted,
            resolved_server,
            resolved_database,
        )

        driver = (
            os.getenv("DB_ODBC_DRIVER")
            or getattr(Config, "DB_ODBC_DRIVER", None)
            or "ODBC Driver 17 for SQL Server"
        )
        if not trusted and (
            not resolved_user or not resolved_password
        ):
            raise ValueError(
                "DB_USER / DB_PASSWORD are required when not using Trusted_Connection."
            )

        self._server = resolved_server
        self._database = resolved_database
        self._user = resolved_user
        self._password = resolved_password
        self._trusted = trusted
        self._driver = driver

        self.connection_string = self._build_connection_string(
            self._database
        )
        self._ensure_database_exists()

        # Initialize connection pool
        self.pool = SimpleConnectionPool(
            self._create_new_connection,
            max_size=10,
        )
        self._initialize_db()

    def _build_connection_string(self, database_name: str) -> str:
        if self._trusted:
            return (
                f"DRIVER={{{self._driver}}};"
                f"SERVER={self._server};"
                f"DATABASE={database_name};"
                "Trusted_Connection=yes;"
                "Encrypt=no;"
                "TrustServerCertificate=yes;"
            )
        return (
            f"DRIVER={{{self._driver}}};"
            f"SERVER={self._server};"
            f"DATABASE={database_name};"
            f"UID={self._user};"
            f"PWD={self._password};"
            "Trusted_Connection=no;"
            "Encrypt=no;"
            "TrustServerCertificate=yes;"
        )

    def _ensure_database_exists(self) -> None:
        if not self._database:
            raise ValueError("DB_NAME is required to initialize the database.")

        try:
            test_conn = pyodbc.connect(
                self._build_connection_string(self._database)
            )
            test_conn.close()
            return
        except pyodbc.Error:
            pass

        try:
            master_conn = pyodbc.connect(
                self._build_connection_string("master"),
                autocommit=True,
            )
            cursor = master_conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM sys.databases WHERE name = ?;",
                (self._database,),
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(f"CREATE DATABASE [{self._database}];")
            master_conn.close()
        except pyodbc.Error as exc:
            logger.exception(
                "Failed to create database %s: %s",
                self._database,
                exc,
            )
            raise

        test_conn = pyodbc.connect(
            self._build_connection_string(self._database)
        )
        test_conn.close()

    def _create_new_connection(self):
        """建立一個新的資料庫連線。"""
        return pyodbc.connect(self.connection_string)

    @contextlib.contextmanager
    def _get_connection(self):
        """取得資料庫連線（使用完畢後會自動歸還）。"""
        conn = self.pool.get_connection()
        try:
            yield conn
            # Commit on success (imitating pyodbc context manager behavior)
            conn.commit()
        except Exception:
            # Rollback on error
            conn.rollback()
            raise
        finally:
            # Always return to pool
            self.pool.return_connection(conn)

    def _initialize_db(self):
        """初始化資料庫：建立所有必要的資料表與索引。"""
        try:
            with self._get_connection() as conn:
                init_cur = conn.cursor()

                # 1. 建立使用者偏好設定表
                user_preferences_cols = """
                    [user_id] NVARCHAR(255) NOT NULL PRIMARY KEY,
                    [language] VARCHAR(50) NULL,
                    [role] NVARCHAR(50) NULL,
                    [is_admin] BIT NULL,
                    [responsible_area] NVARCHAR(255) NULL,
                    [created_at] datetime2(2) NULL,
                    [display_name] NVARCHAR(255) NULL,
                    [last_active] datetime2(2) NULL
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "user_preferences",
                    user_preferences_cols,
                )

                # 2. 建立設備清單表
                equipment_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL PRIMARY KEY,
                    [name] NVARCHAR(255) NOT NULL,
                    [equipment_type] NVARCHAR(255) NULL,
                    [location] NVARCHAR(255) NULL,
                    [status] NVARCHAR(255) NULL,
                    [last_updated] datetime2(2) NULL
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "equipment",
                    equipment_cols,
                )

                # 3. 建立對話紀錄表
                conversations_cols = """
                    [sender_id] NVARCHAR(255) NOT NULL ,
                    [receiver_id] NVARCHAR(255) NOT NULL,
                    [sender_role] NVARCHAR(50) NOT NULL,
                    [content] NVARCHAR(MAX) NOT NULL,
                    [timestamp] datetime2(2) NOT NULL DEFAULT GETDATE()
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "conversations",
                    conversations_cols,
                )
                # 建立索引以加速對話紀錄查詢
                self._create_index_if_not_exists(
                    init_cur,
                    "idx_conversations_sender_timestamp",
                    "conversations",
                    ["sender_id", "[timestamp] DESC"],
                )

                # 4. 建立使用者設備訂閱表
                user_equipment_subscriptions_cols = """
                    [subscription_id] INT IDENTITY(1,1) PRIMARY KEY,
                    [user_id] NVARCHAR(255) NOT NULL,
                    [notification_level] NVARCHAR(50) NULL,
                    [subscribed_at] datetime2(2) NULL DEFAULT GETDATE(),
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    CONSTRAINT FK_subscriptions_user
                        FOREIGN KEY (user_id)
                        REFERENCES user_preferences(user_id),
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

                # 建立索引以加速設備訂閱查詢
                self._create_index_if_not_exists(
                    init_cur,
                    "idx_subscriptions_equipment",
                    "user_equipment_subscriptions",
                    ["equipment_id"],
                )

                # 5. 建立警報歷史紀錄表
                alert_history_cols = """
                    [error_id] INT NOT NULL PRIMARY KEY,
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [detected_anomaly_type] NVARCHAR(255) NULL,
                    [severity] NVARCHAR(255) NULL,
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

                # 6. 建立設備數據指標表
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

                # 7. 建立設備數據閾值表（警報標準）
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

                # 8. 建立錯誤日誌表
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
                    [severity] NVARCHAR(MAX) NULL
                """
                self._create_table_if_not_exists(
                    init_cur, "error_logs", error_logs_cols
                )

                # 9. 建立異常統計表（月報）
                stats_abnormal_monthly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [month] INT NOT NULL,
                    [detected_anomaly_type] NVARCHAR(255) NOT NULL,
                    [total_operation_sec] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    PRIMARY KEY (
                        equipment_id, year, month, detected_anomaly_type
                    )
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_abnormal_monthly",
                    stats_abnormal_monthly_cols,
                )

                # 10. 建立異常統計表（季報）
                stats_abnormal_quarterly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [quarter] INT NOT NULL,
                    [detected_anomaly_type] NVARCHAR(255) NOT NULL,
                    [total_operation_sec] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    PRIMARY KEY (
                        equipment_id, year, quarter, detected_anomaly_type
                    )
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_abnormal_quarterly",
                    stats_abnormal_quarterly_cols,
                )

                # 11. 建立異常統計表（年報）
                stats_abnormal_yearly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [detected_anomaly_type] NVARCHAR(255) NOT NULL,
                    [total_operation_sec] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    PRIMARY KEY (equipment_id, year, detected_anomaly_type)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_abnormal_yearly",
                    stats_abnormal_yearly_cols,
                )

                # 12. 建立運作統計表（月報）
                stats_operational_monthly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [month] INT NOT NULL,
                    [total_operation_sec] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    PRIMARY KEY (equipment_id, year, month)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_operational_monthly",
                    stats_operational_monthly_cols,
                )

                # 13. 建立運作統計表（季報）
                stats_operational_quarterly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL
                        FOREIGN KEY REFERENCES equipment(equipment_id),
                    [year] INT NOT NULL,
                    [quarter] INT NOT NULL,
                    [total_operation_sec] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
                    PRIMARY KEY (equipment_id, year, quarter)
                """
                self._create_table_if_not_exists(
                    init_cur,
                    "stats_operational_quarterly",
                    stats_operational_quarterly_cols,
                )

                # 14. 建立運作統計表（年報）
                stats_operational_yearly_cols = """
                    [equipment_id] NVARCHAR(255) NOT NULL,
                    [year] INT NOT NULL,
                    [total_operation_sec] INT NULL,
                    [downtime_sec] INT NULL,
                    [downtime_rate_percent] NVARCHAR(255) NULL,
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

                # 確保資料庫結構符合最新需求（處理欄位更名等）
                self._apply_docx_schema_migrations(init_cur)

                # conn.commit() # Handled by _get_connection context manager
                logger.info("資料庫初始化完成。")

            self._ensure_system_user()
        except pyodbc.Error as e:
            logger.exception("初始化資料庫時發生錯誤 (ODBC): %s", e)
            raise
        except Exception as ex:
            logger.exception("初始化資料庫時發生未預期的錯誤: %s", ex)
            raise

    def _create_table_if_not_exists(
        self,
        cursor,
        table_name,
        columns_definition,
    ):
        """檢查資料表是否存在，若不存在則建立。"""
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
            logger.info("已建立資料表：%s", table_name)
        else:
            logger.info("資料表 %s 已存在，跳過建立。", table_name)

    def _create_index_if_not_exists(
        self, cursor, index_name, table_name, columns
    ):
        """檢查索引是否存在，若不存在則建立。"""
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
            logger.info("已在 %s 建立索引 %s。", table_name, index_name)
        else:
            logger.info("索引 %s 已存在於 %s。", index_name, table_name)

    def _column_exists(self, cursor, table_name: str, column_name: str) -> bool:
        cursor.execute(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = 'dbo' "
            "AND TABLE_NAME = ? "
            "AND COLUMN_NAME = ?;",
            (table_name, column_name),
        )
        return cursor.fetchone()[0] > 0

    def _rename_column_if_needed(
        self,
        cursor,
        table_name: str,
        old_column: str,
        new_column: str,
    ) -> None:
        """若舊欄位名稱存在且新欄位名稱不存在，則進行更名。"""
        try:
            if self._column_exists(
                cursor, table_name, old_column
            ) and not self._column_exists(cursor, table_name, new_column):
                cursor.execute(
                    "EXEC sp_rename ?, ?, 'COLUMN';",
                    (f"dbo.{table_name}.{old_column}", new_column),
                )
                logger.info(
                    "已將資料表 %s 的欄位 %s 更名為 %s",
                    table_name,
                    old_column,
                    new_column,
                )
        except pyodbc.Error as e:
            logger.warning(
                "欄位更名失敗 (資料表=%s, %s->%s): %s",
                table_name,
                old_column,
                new_column,
                e,
            )

    def _alter_column_nullable(
        self,
        cursor,
        table_name: str,
        column_name: str,
        sql_type: str,
    ) -> None:
        """修改欄位屬性，使其允許空值 (NULL)。"""
        try:
            if self._column_exists(cursor, table_name, column_name):
                cursor.execute(
                    f"ALTER TABLE {table_name} "
                    f"ALTER COLUMN [{column_name}] {sql_type} NULL;"
                )
        except pyodbc.Error as e:
            logger.warning(
                "修改欄位允許空值失敗 (資料表=%s, 欄位=%s): %s",
                table_name,
                column_name,
                e,
            )

    def _apply_docx_schema_migrations(self, cursor) -> None:
        """套用資料庫結構變更（如欄位更名），以相容舊版資料庫。"""
        # 欄位更名
        self._rename_column_if_needed(
            cursor, "alert_history", "severity_level", "severity"
        )
        self._rename_column_if_needed(
            cursor, "error_logs", "severity_level", "severity"
        )

        for table in [
            "stats_abnormal_monthly",
            "stats_abnormal_quarterly",
            "stats_abnormal_yearly",
            "stats_operational_monthly",
            "stats_operational_quarterly",
            "stats_operational_yearly",
        ]:
            self._rename_column_if_needed(
                cursor, table, "total_operation_hrs", "total_operation_sec"
            )

        # 調整欄位允許空值
        self._alter_column_nullable(
            cursor, "equipment", "status", "NVARCHAR(255)"
        )

    def _ensure_system_user(self):
        """確保系統預設使用者（機器人）存在於資料庫中。"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM user_preferences WHERE user_id = ?;",
                    ("bot",),
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute(
                        """
                        INSERT INTO user_preferences
                            (user_id, language, role, is_admin,
                             responsible_area,
                             created_at, display_name, last_active)
                        VALUES (?, ?, ?, 0, NULL, GETDATE(), ?, GETDATE());
                        """,
                        ("bot", "zh-Hant", "assistant", "System Bot"),
                    )
                    # conn.commit() # Handled by _get_connection
        except pyodbc.Error as e:
            logger.exception("建立系統使用者失敗: %s", e)

    def add_message(self, sender_id, receiver_id, sender_role, content):
        """新增一筆對話紀錄。"""
        try:
            with self._get_connection() as conn:
                conv_add_cur = conn.cursor()
                conv_add_cur.execute(
                    """
                    INSERT INTO conversations
                        (sender_id, receiver_id, sender_role, content)
                    VALUES (?, ?, ?, ?);
                    """,
                    (sender_id, receiver_id, sender_role, content),
                )
                # conn.commit() # Handled by _get_connection
            return True
        except pyodbc.Error as e:
            logger.exception("新增對話失敗: %s", e)
            return False

    def get_conversation_history(self, sender_id, limit=10):
        """取得指定使用者的最近對話歷史紀錄。"""
        try:
            with self._get_connection() as conn:
                conv_hist_cur = conn.cursor()
                conv_hist_cur.execute(
                    """
                    SELECT TOP (?) sender_role, content
                    FROM conversations
                    WHERE sender_id = ?
                    ORDER BY timestamp DESC;
                    """,
                    (limit, sender_id),
                )
                messages = [
                    {"role": sender_role, "content": content}
                    for sender_role, content in conv_hist_cur.fetchall()
                ]
                # 因為 SQL 是倒序查詢，這裡反轉回正序
                messages.reverse()
                return messages
        except pyodbc.Error as e:
            logger.exception("取得對話歷史失敗: %s", e)
            return []

    def get_conversation_stats(self):
        """取得對話系統的統計數據（總訊息數、用戶數等）。"""
        try:
            with self._get_connection() as conn:
                conv_stats_cur = conn.cursor()
                conv_stats_cur.execute("SELECT COUNT(*) FROM conversations;")
                total_messages = conv_stats_cur.fetchone()[0]
                conv_stats_cur.execute(
                    "SELECT COUNT(DISTINCT sender_id) FROM conversations;"
                )
                unique_senders = conv_stats_cur.fetchone()[0]
                conv_stats_cur.execute(
                    """
                    SELECT COUNT(*) FROM conversations
                    WHERE timestamp >= DATEADD(day, -1, GETDATE());
                    """
                )
                last_24h = conv_stats_cur.fetchone()[0]
                conv_stats_cur.execute(
                    "SELECT sender_role, COUNT(*) FROM conversations "
                    "GROUP BY sender_role;"
                )
                role_counts = dict(conv_stats_cur.fetchall())
                return {
                    "total_messages": total_messages,
                    "unique_users": unique_senders,
                    "last_24h": last_24h,
                    "user_messages": role_counts.get("user", 0),
                    "assistant_messages": role_counts.get("assistant", 0),
                    "system_messages": role_counts.get("system", 0),
                    "other_messages": sum(
                        count
                        for role, count in role_counts.items()
                        if role not in ("user", "assistant", "system")
                    ),
                }
        except pyodbc.Error as e:
            logger.exception("取得對話統計失敗: %s", e)
            return {
                "total_messages": 0,
                "unique_users": 0,
                "last_24h": 0,
                "user_messages": 0,
                "assistant_messages": 0,
                "system_messages": 0,
                "other_messages": 0,
            }

    def get_recent_conversations(self, limit=20):
        """取得最近有活動的對話列表。"""
        try:
            with self._get_connection() as conn:
                recent_conv_cur = conn.cursor()
                sql_query = """
                SELECT DISTINCT TOP (?)
                    c.sender_id,
                    p.language,
                    MAX(c.timestamp) as last_activity_ts
                FROM conversations c
                LEFT JOIN user_preferences p ON c.sender_id = p.user_id
                GROUP BY c.sender_id, p.language
                ORDER BY last_activity_ts DESC;
                """
                recent_conv_cur.execute(sql_query, (limit,))
                results = []
                for user_id_val, language, timestamp_val in recent_conv_cur.fetchall():
                    recent_conv_cur.execute(
                        "SELECT COUNT(*) FROM conversations WHERE sender_id = ?;",
                        (user_id_val,),
                    )
                    message_count = recent_conv_cur.fetchone()[0]
                    recent_conv_cur.execute(
                        """
                        SELECT TOP 1 content FROM conversations
                        WHERE sender_id = ? AND sender_role = 'user'
                        ORDER BY timestamp DESC;
                        """,
                        (user_id_val,),
                    )
                    last_message_content = recent_conv_cur.fetchone()
                    results.append(
                        {
                            "user_id": user_id_val,
                            "language": language or "zh-Hant",
                            "last_activity": timestamp_val,
                            "message_count": message_count,
                            "last_message": (
                                last_message_content[0] if last_message_content else ""
                            ),
                        }
                    )
                return results
        except pyodbc.Error as e:
            logger.exception("取得最近對話失敗: %s", e)
            return []

    def set_user_preference(self, user_id, language=None, role=None):
        """設定或更新使用者的偏好設定（語言、角色）。"""
        try:
            with self._get_connection() as conn:
                user_pref_set_cur = conn.cursor()
                user_pref_set_cur.execute(
                    "SELECT user_id FROM user_preferences WHERE user_id = ?;",
                    (user_id,),
                )
                user_exists = user_pref_set_cur.fetchone()

                if user_exists:
                    update_parts = []
                    params = []
                    if language is not None:
                        update_parts.append("language = ?")
                        params.append(language)
                    if role is not None:
                        update_parts.append("role = ?")
                        params.append(role)

                    if not update_parts:
                        user_pref_set_cur.execute(
                            (
                                "UPDATE user_preferences "
                                "SET last_active = GETDATE() "
                                "WHERE user_id = ?;"
                            ),
                            (user_id,),
                        )
                    else:
                        sql = (
                            "UPDATE user_preferences "
                            "SET last_active = GETDATE(), "
                            + ", ".join(update_parts)
                            + " WHERE user_id = ?;"
                        )
                        params.append(user_id)
                        user_pref_set_cur.execute(sql, tuple(params))
                else:
                    user_pref_set_cur.execute(
                        """
                        INSERT INTO user_preferences
                            (user_id, language, role, last_active,
                             is_admin, responsible_area)
                        VALUES (?, ?, ?, GETDATE(), 0, NULL);
                        """,
                        (user_id, language or "zh-Hant", role or "user"),
                    )
                return True
        except pyodbc.Error as e:
            logger.exception("設定使用者偏好失敗: %s", e)
            return False

    def get_user_preference(self, user_id):
        """取得使用者的偏好設定，若不存在則建立預設值。"""
        try:
            with self._get_connection() as conn:
                user_pref_get_cur = conn.cursor()
                user_pref_get_cur.execute(
                    "SELECT language, role, is_admin, responsible_area "
                    "FROM user_preferences WHERE user_id = ?;",
                    (user_id,),
                )
                result = user_pref_get_cur.fetchone()
                if result:
                    return {
                        "language": result[0],
                        "role": result[1],
                        "is_admin": result[2],
                        "responsible_area": result[3],
                    }
                logger.info(
                    "找不到使用者 %s 的偏好設定，將建立預設值。",
                    user_id,
                )
                self.set_user_preference(user_id)
                return {
                    "language": "zh-Hant",
                    "role": "user",
                    "is_admin": False,
                    "responsible_area": None,
                }
        except pyodbc.Error as e:
            logger.exception("取得使用者偏好失敗: %s", e)
            return {
                "language": "zh-Hant",
                "role": "user",
                "is_admin": False,
                "responsible_area": None,
            }
    def insert_alert_history(self, log_data: dict):
        """新增一筆警報歷史紀錄與錯誤日誌。"""
        sql_alert_history = """
        INSERT INTO alert_history (
            error_id, equipment_id, detected_anomaly_type,
            severity, created_time
        ) VALUES (?, ?, ?, ?, ?);
        """

        sql_error_log = """
        INSERT INTO error_logs (
            log_date, error_id, equipment_id, deformation_mm,
            rpm, event_time, detected_anomaly_type, severity
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """

        sql_get_max = "SELECT ISNULL(MAX(error_id), 0) FROM alert_history;"

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute(sql_get_max)
                latest_error_id = cursor.fetchone()[0] + 1
                event_time = datetime.datetime.now()

                severity = log_data.get("severity")
                if severity is None:
                    severity = log_data.get("severity_level")

                cursor.execute(
                    sql_alert_history,
                    latest_error_id,
                    log_data["equipment_id"],
                    log_data["detected_anomaly_type"],
                    severity,
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
                    severity,
                )

                logger.info(
                    "已新增警報紀錄 (設備ID: %s)",
                    log_data["equipment_id"],
                )
                return {
                    "error_id": latest_error_id,
                    "created_time": event_time,
                }
        except pyodbc.Error as ex:
            logger.error("新增警報失敗: %s", ex)
            raise

    def get_alert_info(self, error_id: int, detected_anomaly_type: str):
        """查詢特定警報的詳細資訊。"""
        sql = (
            "SELECT equipment_id, detected_anomaly_type FROM alert_history "
            "WHERE error_id = ? AND detected_anomaly_type = ?;"
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
                "查詢警報資訊失敗 (error_id: %s, detected_anomaly_type: %s): %s",
                error_id,
                detected_anomaly_type,
                e,
            )
            return None

    def resolve_alert_history(self, log_data: dict):
        """將警報標記為已解決，並更新解決時間與備註。"""
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
                        "警報已解決 (error_id: %s, detected_anomaly_type: %s, equipment_id: %s)",
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
                        "警報先前已解決 (error_id: %s, equipment_id: %s, "
                        "detected_anomaly_type: %s)",
                        log_data["error_id"],
                        log_data["equipment_id"],
                        log_data["detected_anomaly_type"],
                    )
                    return (already_resolved_time[0], "already_resolved")

                logger.warning(
                    "找不到對應的未解決警報 (error_id: %s, "
                    "detected_anomaly_type: %s, equipment_id: %s)",
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
                "解決警報時發生錯誤 (error_id: %s, detected_anomaly_type: %s, "
                "equipment_id: %s): %s",
                error_id_val,
                detected_anomaly_type_val,
                equipment_id_val,
                ex,
            )
            raise

    def get_subscribed_users(self, equipment_id: str):
        """取得訂閱了指定設備的所有使用者 ID。"""
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
            logger.error("取得訂閱使用者失敗 (%s): %s", equipment_id, e)
            return []

    def get_user_subscriptions(self, user_id: str):
        """取得指定使用者訂閱的所有設備 ID。"""
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
            logger.error("取得使用者訂閱失敗 (%s): %s", user_id, e)
            return []


# 根據環境變數決定是否初始化資料庫物件（測試模式下不初始化）
if os.environ.get("TESTING", "False").lower() != "true":
    db = Database()
else:
    db = None

if __name__ == "__main__":
    if db is None:
        db = Database()
    logger.info("Database initialization complete.")
