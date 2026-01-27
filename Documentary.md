# 專案文件

## 概述

本專案以 LINE Bot 作為操作介面，提供設備監控的四大功能：警報發送與解決、設備訂閱、報表查詢與設備狀態查看。  
系統接收外部設備警報資料並存入 SQL Server，依據訂閱關係推播通知給使用者，並透過 LINE 指令完成查詢與管理。  
回覆採用指令/關鍵字規則式流程，目前未整合 AI 問答服務。

## 技術棧

- **Python 3.11**
- **Flask**
- **LINE Bot SDK v3.x**
- **SQL Server** (pyodbc)
- **Flask-Talisman**
- **Pandas / Openpyxl**（初始資料匯入）

## 主要功能

1. **警報發送與解決**
   - 外部設備透過 REST API 發送警報 (`POST /alarms`)
   - 維修完成後透過 REST API 解決警報 (`POST /resolvealarms`)
   - 自動推播警報/解決訊息給訂閱者

2. **訂閱設備**
   - 使用者透過 LINE 指令訂閱/取消訂閱設備
   - 查詢已訂閱設備清單

3. **查詢報表**
   - 以文字摘要回覆營運總覽與異常分項
   - 支援月報 / 季報 / 年報

4. **查看設備狀態**
   - 設備狀態摘要（依設備類型統計）
   - 設備詳情（最新指標、未解決警報）

## 專案架構

```
.
├── app.py                      # Flask 應用程式進入點
├── src/
│   ├── routes/
│   │   ├── alarm.py            # 警報 API
│   │   └── main.py             # LINE webhook
│   ├── services/
│   │   └── line_service.py     # LINE 推播/回覆
│   ├── config.py               # 環境變數設定
│   ├── database.py             # 資料庫操作
│   ├── initial_data.py         # Excel 初始資料匯入
│   ├── linebot_connect.py      # LINE 事件處理
│   └── reply.py                # LINE 指令回覆
├── tool/
│   ├── send_alarm.py
│   └── resolve_alarm.py
├── requirements.txt
└── README.md
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

## 資料庫結構 (核心資料表)

- **equipment**：設備基本資訊
- **equipment_metrics**：設備指標資料
- **alert_history**：警報紀錄
- **error_logs**：警報事件記錄
- **user_equipment_subscriptions**：使用者訂閱關係
- **stats_operational_* / stats_abnormal_***：報表統計

## 啟動與使用

### 安裝套件

```bash
pip install -r requirements.txt
```

### 啟動服務

```bash
python app.py
```

### LINE 指令一覽

- **help / 幫助 / 選單**：顯示功能選單  
- **使用說明 / 指南**：顯示使用說明  
- **查看報表 / 報表**：以文字顯示報表摘要  
- **訂閱設備**：查看可訂閱設備並訂閱  
- **取消訂閱 [設備ID]**：取消訂閱  
- **我的訂閱**：查看已訂閱設備  
- **設備狀態 / 機台狀態**：查看設備狀態摘要  
- **設備詳情 [設備名稱或ID]**：查看設備詳情  

### 警報 API

- **送出警報**：`POST /alarms`
  - 必填：`equipment_id`、`detected_anomaly_type`、`severity_level`
  - 可選：`rpm`、`deformation_mm`

- **解決警報**：`POST /resolvealarms`
  - 必填：`equipment_id`、`resolved_by`
  - 可選：`error_id`、`detected_anomaly_type`、`resolution_notes`
  - 若未提供 `error_id`，系統會以 `equipment_id` 找到最新未解決警報並解決

## 安全性考量

- LINE webhook 簽名驗證
- 敏感資訊透過環境變數管理
- Flask-Talisman 內容安全政策

## 常見問題

1. **LINE 簽名驗證失敗**  
   檢查 `LINE_CHANNEL_SECRET` 是否正確。

2. **警報未寫入資料庫**  
   確認 SQL Server 連線字串與帳號密碼設定。

3. **無法接收警報通知**  
   確認使用者已訂閱該設備，並檢查 LINE Token 是否有效。
