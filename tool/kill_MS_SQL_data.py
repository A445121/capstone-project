import pyodbc
import os

# 1. 資料庫連線設定
DB_SERVER = os.getenv("DB_SERVER", "localhost")  # Default
DB_NAME = os.getenv("DB_NAME", "Project")  # Default
DB_USER = os.getenv("DB_USER")  # Default
DB_PASSWORD = os.getenv("DB_PASSWORD")# For potential future use
driver = '{ODBC Driver 17 for SQL Server}'

# 2. 建立連線
connection_string = (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            f"SERVER={DB_SERVER};"
            f"DATABASE={DB_NAME};"
            "Trusted_Connection=yes;"
        )

try:
    conn = pyodbc.connect(connection_string)
    cursor = conn.cursor()
    print(f" 已連線：{DB_SERVER} / 資料庫：{DB_NAME}")

    # 3. 關閉外鍵限制
    print("⚠️ 關閉所有外鍵約束...")
    cursor.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? NOCHECK CONSTRAINT all'")

    # 4. 清空資料表
    print("🧹 清空所有資料表...")
    cursor.execute("EXEC sp_MSforeachtable 'DELETE FROM ?'")

    # 5. 重啟外鍵約束
    print("🔒 重新啟用外鍵約束...")
    cursor.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? WITH CHECK CHECK CONSTRAINT all'")

    # 6. 提交與關閉
    conn.commit()
    print("✅ 清空完成（保留資料表結構）")

except Exception as e:
    print(f"❌ 執行錯誤：{e}")

finally:
    if 'cursor' in locals(): cursor.close()
    if 'conn' in locals(): conn.close()
