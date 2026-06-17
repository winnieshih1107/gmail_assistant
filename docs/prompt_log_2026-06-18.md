# 開發紀錄 — 2026-06-18

紀錄「Gmail 整理小助手」從零開始建置的需求演進與每次調整。

## 1. 建立 Gmail 整理助手

需求：建立一個 Gmail 整理小助手，依寄件者分類到不同資料夾。

- 使用 Gmail API（OAuth2 認證，不需付費，個人用量在免費配額內）
- 核心邏輯（`gmail_organizer.py`）：
  - `authenticate()`：OAuth2 認證，第一次開啟瀏覽器授權，後續沿用 `token.json`
  - `fetch_senders()`：掃描指定查詢條件的信件，依寄件者分組回傳 `{(name, email, domain): [msg_ids]}`
  - `ensure_label()`：若 Gmail 標籤不存在則建立，快取避免重複 API 呼叫
  - `apply_labels_to_senders()`：建立巢狀標籤（`寄件者/網域/名稱`）並批次套用
- GUI（`gmail_organizer_gui.py`）：tkinter 桌面視窗，寄件者勾選清單、進度顯示、暫停/停止控制

## 2. 新增群組標籤：不同寄件者歸入同一標籤

需求：多個不同寄件者可以歸類在同個標籤內。

- `apply_labels_to_senders()` 加入 `group_label` 參數
  - 有填：所有選取寄件者的信件統一貼到同一個自訂標籤
  - 留空：維持原本各自建立巢狀標籤的行為
- 將重複的批次套用邏輯抽出成 `_batch_apply()` 內部函式
- GUI 加入「群組標籤（選填）」輸入欄，並在確認對話框顯示不同的說明文字
- 兩種模式可混搭：部分寄件者用群組標籤，其他用各自分類

## 3. 安全性：排除 OAuth 憑證

- 建立 `.gitignore` 排除 `credentials.json`（OAuth 用戶端憑證）與 `token.json`（授權 token）
- 兩個檔案均不進版本控制，避免憑證外洩

## 4. 推送至 GitHub

- 建立獨立 repo：`https://github.com/winnieshih1107/gmail_assistant`
- 撰寫 README.md（功能說明、設定步驟、使用方式、常用搜尋語法）
- 撰寫本開發紀錄
