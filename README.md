# gmail_assistant — Gmail 整理小助手

依寄件者自動在 Gmail 建立巢狀標籤，把收件匣的信件分類到對應資料夾。

## 功能

1. **登入 Google 帳號**：OAuth2 授權，第一次開啟瀏覽器授權，之後自動沿用 `token.json`
2. **掃描信件**：輸入 Gmail 搜尋語法（如 `in:inbox`、`is:unread`），掃描指定信件
3. **寄件者分析**：統計每位寄件者的信件數，列表顯示並依信件數排序
4. **套用標籤**：兩種模式
   - **各自分類**（群組標籤欄留空）：自動建立 `寄件者/網域/名稱` 巢狀標籤
   - **群組標籤**（填入自訂名稱）：多個寄件者的信件統一歸入同一個標籤
5. **批次控制**：套用過程可隨時暫停／繼續／停止

## 標籤結構範例

```
寄件者/
  google.com/
    Google
    Google Workspace
  github.com/
    GitHub
  自訂群組/          ← 群組標籤模式
    電商訂單
```

## 安裝

```bash
pip install -r requirements.txt
```

> 不需要付費。Gmail API 屬於免費配額，個人使用不會產生任何費用。

## 第一次使用設定（只做一次）

1. 前往 [Google Cloud Console](https://console.cloud.google.com)，建立新專案
2. 啟用 **Gmail API**
3. 建立憑證 → OAuth 2.0 用戶端 ID → **桌面應用程式**
4. 下載 `credentials.json`，放到本專案資料夾（已被 `.gitignore` 排除，不會上傳）

## 使用方式

```bash
# GUI 版本（建議）
python gmail_organizer_gui.py

# CLI 版本
python gmail_organizer.py "in:inbox" 200
```

### GUI 操作流程

1. 點「登入 Google 帳號」→ 瀏覽器開啟授權頁，完成後自動關閉
2. 在「Gmail 搜尋語法」欄輸入條件（預設 `in:inbox`），設定最多掃描封數
3. 點「分析寄件者」→ 下方出現寄件者清單（含信件數）
4. 勾選要整理的寄件者
   - 若要讓多個寄件者共用同一標籤，在「群組標籤」欄填入標籤名稱
   - 留空則依網域自動分類
5. 點「套用標籤」→ 確認後開始執行，進度即時顯示在下方

## 常用搜尋語法

| 語法 | 說明 |
|---|---|
| `in:inbox` | 收件匣 |
| `in:all` | 所有信件（含已封存） |
| `is:unread` | 未讀信件 |
| `from:@gmail.com` | 特定網域的來信 |
| `older_than:1y` | 超過一年的舊信 |
| `is:unread in:inbox` | 收件匣未讀 |

## 開發紀錄

詳見 [`docs/prompt_log_2026-06-18.md`](docs/prompt_log_2026-06-18.md)。
