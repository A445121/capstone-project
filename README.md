# LINE Bot 設備監控系統

## 主要功能

- **發送/解決警報**：設備透過 REST API 送出警報與解決訊息
- **訂閱設備**：使用者可在 LINE 訂閱/取消訂閱設備
- **查詢報表**：在 LINE 以文字訊息回覆營運與異常統計
- **查看設備狀態**：查看設備狀態摘要與設備詳情

## 技術棧

- **Python 3.11**
- **Flask**
- **LINE Bot SDK v3.x**
- **SQL Server** (pyodbc)
- **Flask-Talisman**

## 專案架構

```
.
├── app.py                      # Flask 應用程式進入點
├── src/                         # 主要源碼
│   ├── routes/                  # 路由模組
│   │   ├── alarm.py             # 警報 API
│   │   └── main.py              # LINE webhook
│   ├── services/                # 業務邏輯模組
│   │   └── line_service.py      # LINE 推播/回覆
│   ├── config.py                # 環境變數設定
│   ├── database.py              # 資料庫操作
│   ├── initial_data.py          # Excel 初始資料匯入
│   ├── linebot_connect.py       # LINE 事件處理
│   └── reply.py                 # LINE 指令回覆
├── tool/                        # 測試工具
│   ├── send_alarm.py
│   └── resolve_alarm.py
├── requirements.txt             # 相依套件列表
└── Documentary.md               # 詳細專案文件
```

## 環境設定

建立 `.env` 檔案並設定以下環境變數：

```
# 一般設定
FLASK_DEBUG=False
PORT=5000
SECRET_KEY=your_secret_key
VALIDATION_MODE=strict
Reverse_Proxy=
SSL_CERT_PATH=certs/cert.pem
SSL_KEY_PATH=certs/key.pem

# LINE Bot API
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
LINE_CHANNEL_SECRET=your_line_channel_secret

# 資料庫設定 (SQL Server)
DB_SERVER=localhost
Windows_login=
DB_NAME=Project
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_ODBC_DRIVER=ODBC Driver 17 for SQL Server
```

## 安裝相依套件

```bash
pip install -r requirements.txt
```

## 執行方式

```bash
python app.py
```

> 註：直接執行 `app.py` 預設監聽 PORT 443 (若權限允許) 或可透過環境變數指定。

## LINE Bot 指令

- **help / 幫助 / 選單**：顯示功能選單
- **使用說明 / 指南**：顯示使用說明
- **查看報表 / 報表**：以文字顯示報表摘要
- **訂閱設備**：查看可訂閱設備並訂閱
- **取消訂閱 [設備ID]**：取消訂閱
- **我的訂閱**：查看已訂閱設備
- **設備狀態 / 機台狀態**：查看設備狀態摘要
- **設備詳情 [設備名稱或ID]**：查看設備詳情

## 警報與通知功能

- **送出警報**：`POST /alarms`
- **解決警報**：`POST /resolvealarms`
  - 若未提供 `error_id`，系統會以 `equipment_id` 取得最新未解決警報並解決

## 問題排解

- **LINE 簽名驗證失敗**：檢查 `LINE_CHANNEL_SECRET` 是否正確
- **設備監控問題**：確認資料庫連線正常，並確認外部設備是否正確發送 POST 至 `/alarms`

## 相關資源

- [LINE 開發者平台](https://developers.line.biz/zh-hant/)
- [Flask 文件](https://flask.palletsprojects.com/)
