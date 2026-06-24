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

## 5. 標籤管理：重新命名／刪除／更換標籤（2026-06-24）

需求：信件標籤要能修改。

- `gmail_organizer.py` 新增：
  - `list_user_labels()`：列出使用者自訂標籤（排除系統標籤）與信件數
  - `rename_label()` / `delete_label()`：包裝 Gmail API 的 `labels().patch` / `labels().delete`
  - `list_message_ids()`：僅列出符合查詢條件的信件 ID（輕量，不抓寄件者資訊）
  - `swap_label_on_messages()`：把目前帶有某標籤的信件整批換成另一個標籤（新增新標籤＋移除舊標籤）
  - 把原本的 `_batch_apply()` 改寫成更通用的 `_batch_modify()`，可同時新增／移除多個標籤
- GUI 改成 `ttk.Notebook` 三分頁結構，新增「標籤管理」分頁：標籤列表（含信件數）、重新命名、刪除、更換標籤
- 注意事項：重新命名巢狀標籤時，子標籤不會自動跟著改路徑（Gmail 標籤名稱本身就是路徑）

## 6. 自動歸類：背景監看收件匣，新信自動套用標籤（2026-06-24）

需求：收件夾可以自動歸類信件，不用每次手動勾選套用。

- `gmail_organizer.py` 新增：
  - 規則持久化：`rules.json`（`load_rules()` / `save_rules()` / `add_rule()` / `match_rule()`），規則可比對寄件者完整地址或網域，對應一個標籤與是否同時歸檔
  - `apply_labels_to_senders()` 加入 `save_as_rule` / `rule_archive` 參數：手動套用標籤時可直接把這次的寄件者→標籤對應存成規則
  - 背景監看：用 Gmail History API（`users.history.list`）取得「自上次檢查後新增到收件匣」的信件，避免每次重新掃描整個收件匣
    - `get_current_history_id()` / `load_watch_state()` / `save_watch_state()`：把目前同步進度（history_id）存到 `watch_state.json`，跨次啟動可接續
    - `fetch_new_inbox_message_ids()`：抓取新信 ID；`get_message_sender()`：取得寄件者
    - `watch_inbox()`：背景迴圈，依規則自動套用標籤（可選擇同時移除 `INBOX` 標籤＝歸檔），history_id 過期（404）時會自動重新校正基準點
- GUI 新增「自動歸類」分頁：規則清單（新增／刪除）、檢查間隔設定、啟動／停止監看按鈕
- `rules.json`、`watch_state.json` 屬個人本機設定，加入 `.gitignore`

## 7. 修復連線錯誤、查詢已建立標籤、寄件者清單顯示歸類狀態（2026-06-24）

實際連上使用者帳號測試時發現兩個問題：

- **背景回呼的例外變數遺失**：多處 `except Exception as e:` 搭配 `self.root.after(0, lambda: ...e...)` 延遲執行，Python 在 `except` 區塊結束時會自動清掉 `e`，導致 lambda 真正執行時 `NameError: cannot access free variable 'e'`，蓋掉了原本的錯誤訊息。修正方式：在 `except` 區塊內先把訊息存成一般變數（`msg = str(e)`）再讓 lambda 捕捉該變數。
- **共用連線物件損壞後一直回傳空結果**：App 原本整個生命週期共用同一個 `self.service`（同一個 `httplib2` 連線池）。一旦某次請求中途撞到底層 SSL 連線異常（`[SSL: WRONG_VERSION_NUMBER]`），之後用同一個物件發出的請求會「成功但回傳空結果」（不丟例外，重試也救不了）。修正方式：把所有實際操作改成在各自的背景執行緒裡呼叫 `authenticate()` 重新建立連線，不再長期共用一個連線物件；`watch_inbox()` 也改成接收 `get_service` callable，每輪監看都重新建立連線。

新增功能：

- 「整理信件」分頁新增「查詢已建立的標籤」下拉選單：登入後自動載入現有標籤，選擇後按「套用到查詢」可直接把搜尋語法設成 `label:"標籤名稱"`，方便重新查詢已分類過的信件
- 寄件者清單每一筆會顯示「目前已歸類在哪個標籤」，沒有標籤則留空：`fetch_senders()` 新增 `label_ids_out` 參數，掃描時順手記錄每位寄件者信件上的標籤 ID（不額外耗費 API 呼叫），搭配新函式 `list_label_id_name_map()` 轉成標籤名稱顯示
