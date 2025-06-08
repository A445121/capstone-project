import logging
import pyodbc
from database import db

# 初始化日誌記錄器
logger = logging.getLogger(__name__)


class EquipmentMonitor:
    """
    半導體設備監控與異常偵測器 (僅限切割機)。

    這個類別負責從資料庫中讀取切割機的各項監控指標，
    根據預先設定的閾值判斷設備是否出現異常，
    並在偵測到異常時記錄警報、更新設備狀態，以及發送通知給相關人員。
    """

    # 設備類型常數 (只保留切割機)
    DICER = "dicer"  # 切割機

    # 嚴重程度常數
    SEVERITY_WARNING = "warning"    # 警告
    SEVERITY_CRITICAL = "critical"  # 嚴重
    SEVERITY_EMERGENCY = "emergency"  # 緊急

    def __init__(self):
        """
        初始化 EquipmentMonitor 實例。

        - 建立資料庫接口。
        - 定義設備類型和指標。
        - 從資料庫載入所有指標的判斷閾值。
        """
        self.db = db  # 這裡的 db 已經是 MS SQL Server 的接口
        self.equipment_type_names = {
            self.DICER: "切割機",
        }
        # 這些指標現在會從資料庫的 equipment_metric_thresholds 表中獲取標準
        self.equipment_metrics = {
            self.DICER: ["變形量(mm)", "轉速", "刀具裂痕"],  # 增加刀具裂痕
        }
        # 用於儲存從資料庫載入的閾值
        self.metric_thresholds_data = {}
        self._load_metric_thresholds_from_db()  # 初始化時從資料庫載入標準

    def _load_metric_thresholds_from_db(self):
        """從資料庫的 equipment_metric_thresholds 表中載入所有指標的閾值。"""
        try:
            with self.db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        metric_type, normal_value,
                        warning_min, warning_max,
                        critical_min, critical_max,
                        emergency_min, emergency_max, emergency_op
                    FROM equipment_metric_thresholds;
                """)
                rows = cursor.fetchall()
                if not rows:
                    logger.warning(
                        "資料庫中 equipment_metric_thresholds 表無閾值數據。"
                    )

                # 先清空舊資料，再載入新資料
                self.metric_thresholds_data = {}
                for row in rows:
                    (metric_type, normal_value,
                     w_min, w_max,
                     c_min, c_max,
                     e_min, e_max, e_op) = row

                    self.metric_thresholds_data[metric_type] = {
                        "normal_value": normal_value,
                        "warning": {"min": w_min, "max": w_max},
                        "critical": {"min": c_min, "max": c_max},
                        "emergency": {"min": e_min, "max": e_max, "op": e_op}
                    }
                logger.info(
                    f"成功從資料庫載入 {len(self.metric_thresholds_data)} 個指標的閾值。"
                )
        except pyodbc.Error as db_err:
            logger.exception(f"從資料庫載入閾值時發生錯誤: {db_err}")
            self.metric_thresholds_data = {}  # 清空，避免使用不完整的數據
        except Exception as e:
            logger.exception(f"載入閾值時發生非預期錯誤: {e}")
            self.metric_thresholds_data = {}

    def check_all_equipment(self):
        """
        檢查所有在線的切割機設備是否有異常。

        這是主要的執行入口點。它會重新載入最新的閾值，
        然後遍歷所有非離線狀態的切割機，並逐一檢查它們的指標。
        """
        # 在每次檢查前重新載入閾值，以確保是最新的（如果資料庫有更新）
        self._load_metric_thresholds_from_db()

        try:
            with self.db._get_connection() as conn:  # 正確使用 MS SQL Server 連線
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT equipment_id, name, eq_type FROM equipment "
                    "WHERE status <> 'offline' AND eq_type = ?;",
                    (self.DICER,)
                )
                equipments = cursor.fetchall()
                for equipment_id, name, eq_type in equipments:
                    self._check_equipment_metrics(
                        conn, equipment_id, name, eq_type
                    )
                logger.info("所有切割機設備檢查完成。")
        except pyodbc.Error as db_err:  # 捕獲 pyodbc.Error
            logger.exception(f"檢查所有切割機設備時發生資料庫錯誤: {db_err}")
        except Exception as e:
            logger.exception(f"檢查所有切割機設備時發生非預期錯誤: {e}")

    def _check_equipment_metrics(self, conn, equipment_id, name, eq_type):
        """
        檢查單一設備的所有監控指標是否異常。

        - 使用 SQL 的 `ROW_NUMBER()` 取得過去30分鐘內每種指標的最新一筆數據。
        - 對於獲取到的每個指標，調用 `_determine_severity` 來判斷其嚴重等級。
        - 如果有異常，則匯總所有異常信息，更新設備狀態，記錄到歷史警報，並發送通知。
        - 如果沒有異常，但設備先前處於異常狀態，則將其狀態恢復為 'normal'。
        """
        try:
            cursor = conn.cursor()
            # SQL 查詢：只選擇需要的欄位，並用 ROW_NUMBER() 取得最新的指標數據
            sql_get_metrics = """
                WITH RankedMetrics AS (
                    SELECT
                        id, equipment_id, metric_type, status,
                        value, unit, timestamp,
                        ROW_NUMBER() OVER(
                            PARTITION BY equipment_id, metric_type
                            ORDER BY timestamp DESC
                        ) as rn
                    FROM equipment_metrics
                    WHERE equipment_id = ?
                    AND timestamp > DATEADD(minute, -30, GETDATE())
                )
                SELECT id, equipment_id, metric_type, status,
                       value, unit, timestamp
                FROM RankedMetrics
                WHERE rn = 1;
            """
            cursor.execute(sql_get_metrics, (equipment_id,))

            latest_metrics = {}
            for metric_row in cursor.fetchall():
                _id, _eq_id, metric_type, status, value, unit, ts = metric_row
                latest_metrics[metric_type] = {
                    "value": float(value) if value is not None else None,
                    "unit": unit,
                    "timestamp": ts,
                    "status_from_metric": status
                }

            if not latest_metrics:
                logger.debug(
                    f"設備 {name} ({equipment_id}) 在過去30分鐘內沒有新的監測指標。"
                )
                return

            anomalies = self._collect_anomalies(latest_metrics)

            if anomalies:
                highest_severity = self._get_highest_severity(anomalies)
                full_message = self._format_anomaly_message(
                    equipment_id, name, highest_severity, anomalies
                )

                # 記錄每條異常到 alert_history
                for anomaly in anomalies:
                    self._log_anomaly_to_db(cursor, equipment_id, anomaly)

                self._update_equipment_status(
                    conn, equipment_id, highest_severity, full_message
                )
                conn.commit()
                self._send_alert_notification(
                    equipment_id, full_message, highest_severity
                )
                logger.info(
                    f"設備 {name} ({equipment_id}) 異常已記錄及通知 "
                    f"({highest_severity})。"
                )
            else:
                self._handle_recovery_status(cursor, conn, equipment_id, name)

        except pyodbc.Error as db_err:
            logger.error(
                f"檢查設備 {name} ({equipment_id}) 指標時發生資料庫錯誤: {db_err}"
            )
        except Exception as e:
            logger.error(
                f"檢查設備 {name} ({equipment_id}) 指標時發生未知錯誤: {e}"
            )

    def _collect_anomalies(self, latest_metrics):
        """從最新的指標數據中收集所有異常情況。"""
        anomalies = []
        for metric_type, data in latest_metrics.items():
            # 只處理 self.equipment_metrics 中為 DICER 定義的指標
            is_valid_metric = (
                metric_type in self.equipment_metrics.get(self.DICER, []) and
                data["value"] is not None
            )
            if is_valid_metric:
                severity = self._determine_severity(metric_type, data["value"])
                if severity:
                    anomalies.append({
                        "metric": metric_type,
                        "value": data["value"],
                        "unit": data["unit"],
                        "severity": severity,
                        "timestamp": data["timestamp"]
                    })
        return anomalies

    def _get_highest_severity(self, anomalies):
        """從異常列表中找出最高的嚴重等級。"""
        return max(
            (a["severity"] for a in anomalies),
            key=self._severity_level,
            default=self.SEVERITY_WARNING
        )

    def _format_anomaly_message(
        self, equipment_id, name, highest_severity, anomalies
    ):
        """格式化用於通知的完整異常訊息。"""
        anomaly_messages = []
        for anomaly in anomalies:
            ts_str = (
                anomaly['timestamp'].strftime('%H:%M:%S')
                if anomaly.get('timestamp') else 'N/A'
            )
            metric_info = self.metric_thresholds_data.get(
                anomaly["metric"], {}
            )
            normal_val = metric_info.get("normal_value")
            msg = self._format_single_anomaly_line(
                anomaly, ts_str, normal_val
            )
            anomaly_messages.append(msg)

        return (
            f"設備 {name} ({equipment_id}) 異常提醒 "
            f"({self._severity_emoji(highest_severity)} {highest_severity.upper()}):\n"
            + "\n".join(anomaly_messages)
        )

    def _format_single_anomaly_line(self, anomaly, ts_str, normal_val):
        """格式化單條異常指標的文字描述。"""
        metric = anomaly['metric']
        value = anomaly['value']
        severity = anomaly['severity'].upper()
        unit = anomaly.get('unit', '')

        if metric == "轉速":
            normal_display = (f"(正常應為 {normal_val:.0f} RPM 左右)"
                              if normal_val is not None else "")
            return (f"指標 {metric} 值 {value:.0f} RPM {normal_display}。"
                    f"偵測為 {severity} 等級異常 (於 {ts_str})")

        if metric in ["變形量(mm)", "刀具裂痕"]:
            normal_display = (f"(正常應為 {normal_val:.3f} mm 以下)"
                              if normal_val is not None else "")
            return (f"指標 {metric} 值 {value:.3f} mm {normal_display}。"
                    f"偵測為 {severity} 等級異常 (於 {ts_str})")

        # 通用格式
        return (f"指標 {metric} 值 {value:.2f} {unit}。"
                f"偵測為 {severity} 等級異常 (於 {ts_str})")

    def _log_anomaly_to_db(self, cursor, equipment_id, anomaly):
        """將單條異常記錄插入到 alert_history 資料庫表中。"""
        alert_msg_for_db = (
            f"{anomaly['metric']} 值 {anomaly['value']:.2f} "
            f"{anomaly.get('unit') or ''} "
            f"(嚴重程度: {anomaly['severity'].upper()})"
        )
        cursor.execute(
            """
            INSERT INTO alert_history
                (equipment_id, alert_type, severity, message, created_at)
            VALUES (?, ?, ?, ?, GETDATE());
            """,
            (
                equipment_id,
                f"{anomaly['metric']}_alert",
                anomaly["severity"],
                alert_msg_for_db
            )
        )

    def _handle_recovery_status(self, cursor, conn, equipment_id, name):
        """處理設備從異常狀態恢復正常的情況。"""
        cursor.execute(
            "SELECT status FROM equipment WHERE equipment_id = ?;",
            (equipment_id,)
        )
        current_status_row = cursor.fetchone()
        if current_status_row and current_status_row[0] not in [
            'normal', 'offline'
        ]:
            logger.info(
                f"設備 {name} ({equipment_id}) 指標已恢復正常，"
                f"先前狀態為 {current_status_row[0]}。"
            )
            self._update_equipment_status(
                conn, equipment_id, "normal", "指標已恢復正常"
            )
            conn.commit()

    def _update_equipment_status(
        self, conn, equipment_id, new_status_key,
        alert_message_for_log="狀態更新"
    ):
        """
        更新設備在資料庫中的狀態，並在狀態改變時記錄日誌。

        - 僅當新狀態與當前狀態不同時才執行更新。
        - 狀態改變會觸發一筆記錄到 `alert_history` 表中。
        """
        status_map = {
            self.SEVERITY_WARNING: "warning",
            self.SEVERITY_CRITICAL: "critical",
            self.SEVERITY_EMERGENCY: "emergency",
            "normal": "normal",
            "offline": "offline",
        }
        db_status = status_map.get(new_status_key, "warning")

        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM equipment WHERE equipment_id = ?;",
            (equipment_id,)
        )
        current_status_row = cursor.fetchone()

        if current_status_row and current_status_row[0] != db_status:
            cursor.execute(
                "UPDATE equipment SET status = ?, last_updated = GETDATE() "
                "WHERE equipment_id = ?;",
                (db_status, equipment_id)
            )

            alert_type = (
                "status_change" if new_status_key != "normal" else "recovery"
            )
            severity_for_log = (
                new_status_key if new_status_key != "normal" else "info"
            )
            is_resolved_log = 1 if new_status_key == "normal" else 0

            cursor.execute(
                """
                INSERT INTO alert_history
                    (equipment_id, alert_type, severity,
                     message, is_resolved, created_at)
                VALUES (?, ?, ?, ?, ?, GETDATE());
                """,
                (
                    equipment_id, alert_type, severity_for_log,
                    alert_message_for_log, is_resolved_log
                )
            )
            logger.info(
                f"設備 {equipment_id} 狀態從 "
                f"{current_status_row[0]} 更新為 {db_status}。"
            )

    def _determine_severity(self, metric_type, value):
        """
        根據從資料庫載入的閾值，判斷給定指標值的嚴重程度。

        判斷順序為：緊急 -> 嚴重 -> 警告。
        返回對應的嚴重性常數，如果數值正常則返回 None。
        """
        val = float(value)
        thresholds = self.metric_thresholds_data.get(metric_type)

        if not thresholds:
            logger.warning(
                f"未找到指標 '{metric_type}' 的閾值數據。無法判斷嚴重程度。"
            )
            return None

        # 優先判斷重度異常 (Emergency)
        emergency_thresh = thresholds.get("emergency", {})
        e_min, e_max, e_op = (emergency_thresh.get(k) for k in
                              ["min", "max", "op"])
        if e_op == '>' and e_min is not None and val > e_min:
            return self.SEVERITY_EMERGENCY
        if e_op == '<' and e_max is not None and val < e_max:
            return self.SEVERITY_EMERGENCY
        if e_op is None and e_min is not None and e_max is not None:
            if e_min <= val <= e_max:
                return self.SEVERITY_EMERGENCY

        # 判斷中度異常 (Critical)
        critical_thresh = thresholds.get("critical", {})
        c_min, c_max = critical_thresh.get("min"), critical_thresh.get("max")
        if c_min is not None and c_max is not None and c_min < val <= c_max:
            return self.SEVERITY_CRITICAL

        # 判斷輕度異常 (Warning)
        warning_thresh = thresholds.get("warning", {})
        w_min, w_max = warning_thresh.get("min"), warning_thresh.get("max")
        if w_min is not None and w_max is not None and w_min < val <= w_max:
            return self.SEVERITY_WARNING

        return None  # 如果不在任何異常區間內，則視為正常

    def _severity_level(self, severity):
        """將嚴重性字串轉換為數字等級以便排序或比較。"""
        levels = {
            self.SEVERITY_WARNING: 1,
            self.SEVERITY_CRITICAL: 2,
            self.SEVERITY_EMERGENCY: 3,
            "info": 0,
        }
        return levels.get(severity, 0)

    def _severity_emoji(self, severity):
        """根據嚴重性返回對應的表情符號，用於美化通知訊息。"""
        emojis = {
            self.SEVERITY_WARNING: "⚠️",
            self.SEVERITY_CRITICAL: "🔴",
            self.SEVERITY_EMERGENCY: "🚨",
            "info": "ℹ️",
            "recovery": "✅"
        }
        return emojis.get(severity, "⚠️")

    def _get_equipment_data(self, equipment_id):
        """從資料庫獲取指定設備的名稱、類型和位置資訊。"""
        try:
            with self.db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name, eq_type, location "
                    "FROM equipment WHERE equipment_id = ?;",
                    (equipment_id,),
                )
                result = cursor.fetchone()
                if result:
                    eq_type = result[1]
                    return {
                        "name": result[0],
                        "type": eq_type,
                        "type_name": self.equipment_type_names.get(
                            eq_type, eq_type
                        ),
                        "location": result[2]
                    }
        except pyodbc.Error as db_err:
            logger.error(
                f"從 _get_equipment_data 獲取設備 {equipment_id} 資料失敗: {db_err}"
            )
        return {
            "name": "未知", "type": "未知",
            "type_name": "未知設備", "location": "未知"
        }

    def _generate_ai_recommendation(self, anomalies, equipment_data):
        """
        產生 AI 增強的異常描述和建議。

        此功能(已停用)會格式化一個詳細的提示(prompt)，
        包含設備資訊和具體的異常數據，
        然後調用外部的 OpenAI 服務來獲取專家級的維護建議。
        """
        # 注意：此函數的實現細節可能需要根據實際的AI服務接口進行調整
        logger.info("AI 建議產生功能目前未啟用。")
        return "AI 建議功能當前不可用。"

    def _send_alert_notification(self, equipment_id, message, severity):
        """
        發送警報通知給所有相關人員。

        - 根據設備 ID 和警報嚴重性，從資料庫查詢需要通知的使用者列表。
        - 通知對象包括：訂閱該設備且通知等級符合的使用者，以及該設備類型
          的負責人/管理員。
        - 調用外部的 `send_notification` 函數（例如 Line Bot）來發送格式化後的訊息。
        """
        try:
            from src.linebot_connect import send_notification

            user_ids_to_notify = self._get_notification_recipients(
                equipment_id, severity
            )

            if not user_ids_to_notify:
                logger.warning(
                    f"設備 {equipment_id} 發生警報，但找不到任何符合條件的通知對象。"
                )
                return

            final_message = (
                f"{self._severity_emoji(severity)} "
                f"設備警報 ({equipment_id}):\n{message}"
            )

            for user_id in user_ids_to_notify:
                if send_notification(user_id, final_message):
                    logger.info(
                        f"警報通知已發送給使用者: {user_id} 針對設備 {equipment_id}"
                    )
                else:
                    logger.error(f"發送警報通知給使用者: {user_id} 失敗")

        except pyodbc.Error as db_err:
            logger.exception(
                f"發送設備 {equipment_id} 的通知時發生資料庫錯誤: {db_err}"
            )
        except ImportError:
            logger.error("無法導入 send_notification 函數。警報無法發送。")
        except Exception as e:
            logger.exception(
                f"發送設備 {equipment_id} 的通知時發生非預期錯誤: {e}"
            )

    def _get_notification_recipients(self, equipment_id, severity):
        """查詢並返回應接收指定警報的所有使用者ID集合。"""
        user_ids_to_notify = set()
        level_map = {
            self.SEVERITY_EMERGENCY: ('all', 'critical', 'emergency'),
            self.SEVERITY_CRITICAL: ('all', 'critical'),
            self.SEVERITY_WARNING: ('all',),
        }
        level_filter = level_map.get(severity, ('all',))

        with self.db._get_connection() as conn:
            cursor = conn.cursor()

            # 1. 根據使用者訂閱獲取
            placeholders = ', '.join(['?'] * len(level_filter))
            sql_subs = (
                f"SELECT user_id FROM user_equipment_subscriptions "
                f"WHERE equipment_id = ? AND notification_level IN ({placeholders});"
            )
            params = [equipment_id] + list(level_filter)
            cursor.execute(sql_subs, params)
            for row in cursor.fetchall():
                user_ids_to_notify.add(row[0])

            # 2. 根據負責區域或管理員身份獲取
            cursor.execute(
                "SELECT eq_type FROM equipment WHERE equipment_id = ?;",
                (equipment_id,)
            )
            eq_info = cursor.fetchone()
            if eq_info:
                eq_type = eq_info[0]
                cursor.execute(
                    "SELECT user_id FROM user_preferences "
                    "WHERE responsible_area = ? OR is_admin = 1;",
                    (eq_type,)
                )
                for row in cursor.fetchall():
                    user_ids_to_notify.add(row[0])

        return user_ids_to_notify
