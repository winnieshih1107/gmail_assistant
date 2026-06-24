"""
Gmail 整理小助手 — 核心邏輯
依寄件者網域與名稱，自動在 Gmail 建立巢狀標籤並套用到對應信件。
標籤結構：寄件者 / 網域 / 寄件者名稱

使用前須準備：
  1. 至 Google Cloud Console 建立專案，啟用 Gmail API
  2. 建立 OAuth 2.0 用戶端憑證（類型：桌面應用程式）
  3. 下載 credentials.json，放到本檔案同一資料夾
  4. pip install google-auth-oauthlib google-api-python-client
"""

import email.utils
import json
import os
import re
import threading
import time
from collections import defaultdict

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(_BASE_DIR, "credentials.json")
TOKEN_FILE = os.path.join(_BASE_DIR, "token.json")
RULES_FILE = os.path.join(_BASE_DIR, "rules.json")
WATCH_STATE_FILE = os.path.join(_BASE_DIR, "watch_state.json")
LABEL_PREFIX = "寄件者"


# ---------------------------------------------------------------------------
# 工作控制
# ---------------------------------------------------------------------------

class JobControl:
    def __init__(self):
        self._pause = threading.Event()
        self._pause.set()
        self._stop = threading.Event()

    def reset(self):
        self._stop.clear()
        self._pause.set()

    def wait_if_paused(self):
        self._pause.wait()

    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def request_pause(self):
        self._pause.clear()

    def request_resume(self):
        self._pause.set()

    def request_stop(self):
        self._stop.set()
        self._pause.set()

    def is_stopped(self) -> bool:
        return self._stop.is_set()


# ---------------------------------------------------------------------------
# 認證
# ---------------------------------------------------------------------------

def authenticate():
    """OAuth2 認證，回傳 Gmail API service。第一次執行會開啟瀏覽器授權視窗。"""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                raise FileNotFoundError(
                    f"找不到 credentials.json\n"
                    f"請至 Google Cloud Console 下載 OAuth 憑證並放到：\n{CREDS_FILE}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# 寄件者解析
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> tuple[str, str]:
    """解析 From header，回傳 (display_name, email_address)。"""
    name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    name = name.strip().strip('"\'') or addr
    return name, addr


def get_domain(email_addr: str) -> str:
    return email_addr.split("@")[-1] if "@" in email_addr else "unknown"


def sanitize_label(name: str) -> str:
    """移除 Gmail 標籤名稱不允許的字元（# % * \\ ^），限制長度。"""
    return re.sub(r'[#%*\\^]', '', name).strip()[:80] or "unknown"


# ---------------------------------------------------------------------------
# 掃描信件
# ---------------------------------------------------------------------------

def fetch_senders(service, query: str = "in:inbox", max_results: int = 200,
                  log=print, control: "JobControl | None" = None,
                  label_ids_out: dict | None = None) -> dict:
    """掃描信件並依寄件者分組。
    回傳 {(display_name, email_addr, domain): [msg_id, ...]}。

    label_ids_out 非 None 時，會把每位寄件者目前信件上的標籤 ID（聯集）寫入該 dict，
    供呼叫端對照標籤名稱，顯示「目前已歸類在哪個標籤」。"""
    log(f"掃描信件（{query}，最多 {max_results} 封）...")
    sender_map: dict = defaultdict(list)
    page_token = None
    fetched = 0

    while fetched < max_results:
        if control and control.is_stopped():
            break
        batch_size = min(100, max_results - fetched)
        params = {"userId": "me", "q": query, "maxResults": batch_size}
        if page_token:
            params["pageToken"] = page_token

        result = service.users().messages().list(**params).execute(num_retries=3)
        messages = result.get("messages", [])
        if not messages:
            break

        for msg in messages:
            if control and control.is_stopped():
                break
            try:
                msg_data = service.users().messages().get(
                    userId="me", id=msg["id"],
                    format="metadata", metadataHeaders=["From"],
                ).execute(num_retries=3)
                headers = {
                    h["name"]: h["value"]
                    for h in msg_data.get("payload", {}).get("headers", [])
                }
                name, addr = parse_sender(headers.get("From", ""))
                domain = get_domain(addr)
                key = (name, addr, domain)
                sender_map[key].append(msg["id"])
                if label_ids_out is not None:
                    label_ids_out.setdefault(key, set()).update(msg_data.get("labelIds", []))
                fetched += 1
                if fetched % 50 == 0:
                    log(f"已掃描 {fetched} 封...")
            except Exception as e:
                log(f"略過 {msg['id']}：{e}")

        page_token = result.get("nextPageToken")
        if not page_token or fetched >= max_results:
            break

    log(f"掃描完成：{fetched} 封、{len(sender_map)} 位寄件者")
    return dict(sender_map)


# ---------------------------------------------------------------------------
# 標籤管理
# ---------------------------------------------------------------------------

def list_existing_labels(service) -> dict[str, str]:
    """回傳 {label_name: label_id}。"""
    result = service.users().labels().list(userId="me").execute(num_retries=3)
    return {lb["name"]: lb["id"] for lb in result.get("labels", [])}


def list_label_id_name_map(service) -> dict[str, str]:
    """回傳 {label_id: label_name}，只含使用者自訂標籤（排除系統標籤），單次 API 呼叫。"""
    result = service.users().labels().list(userId="me").execute(num_retries=3)
    return {lb["id"]: lb["name"] for lb in result.get("labels", []) if lb.get("type") == "user"}


def ensure_label(service, label_name: str, cache: dict[str, str]) -> str:
    """若標籤不存在則建立並快取，回傳 label_id。"""
    if label_name in cache:
        return cache[label_name]
    created = service.users().labels().create(
        userId="me",
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute(num_retries=3)
    cache[label_name] = created["id"]
    return created["id"]


# ---------------------------------------------------------------------------
# 套用標籤
# ---------------------------------------------------------------------------

def _batch_modify(service, msg_ids: list[str],
                  add_label_ids: list[str] | None = None,
                  remove_label_ids: list[str] | None = None,
                  log=print, control: "JobControl | None" = None) -> int:
    """批次新增／移除標籤，每次最多 1000 封，回傳處理數。"""
    body_extra = {}
    if add_label_ids:
        body_extra["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body_extra["removeLabelIds"] = remove_label_ids
    applied = 0
    for i in range(0, len(msg_ids), 1000):
        if control:
            control.wait_if_paused()
            if control.is_stopped():
                break
        batch = msg_ids[i:i + 1000]
        service.users().messages().batchModify(
            userId="me",
            body={"ids": batch, **body_extra},
        ).execute(num_retries=3)
        applied += len(batch)
    return applied


def apply_labels_to_senders(service, sender_msg_map: dict, selected_keys: list,
                             group_label: str = "",
                             log=print, control: "JobControl | None" = None,
                             save_as_rule: bool = False, rule_archive: bool = False) -> int:
    """對選取的寄件者套用標籤，回傳套用總封數。

    group_label 非空時：所有選取寄件者的信件統一套用同一個自訂標籤。
    group_label 為空時：依「寄件者/網域/名稱」自動建立各自的巢狀標籤。
    save_as_rule 為真時：把每位選取寄件者（依信箱地址）的標籤對應存成自動歸類規則，
    之後收件匣收到同寄件者的新信會自動套用同樣的標籤。
    """
    label_cache = list_existing_labels(service)
    total_applied = 0
    rules = load_rules() if save_as_rule else None

    if group_label:
        # ── 群組模式：所有選取寄件者共用同一個標籤 ──────────────
        safe = sanitize_label(group_label.strip())
        label_id = ensure_label(service, safe, label_cache)
        all_ids: list[str] = []
        for key in selected_keys:
            all_ids.extend(sender_msg_map[key])
        names = "、".join(k[0] for k in selected_keys[:3])
        if len(selected_keys) > 3:
            names += f" 等 {len(selected_keys)} 位"
        log(f"群組標籤「{safe}」← {names}，共 {len(all_ids)} 封")
        total_applied = _batch_modify(service, all_ids, add_label_ids=[label_id], log=log, control=control)
        if save_as_rule:
            for key in selected_keys:
                rules = add_rule(rules, "address", key[1], safe, rule_archive)
    else:
        # ── 各自分類模式：依網域/名稱建立巢狀標籤 ───────────────
        ensure_label(service, LABEL_PREFIX, label_cache)
        for key in selected_keys:
            if control and control.is_stopped():
                log("已停止。")
                break
            name, addr, domain = key
            parent = f"{LABEL_PREFIX}/{sanitize_label(domain)}"
            label_name = f"{parent}/{sanitize_label(name)}"
            ensure_label(service, parent, label_cache)
            label_id = ensure_label(service, label_name, label_cache)
            msg_ids = sender_msg_map[key]
            log(f"套用「{label_name}」→ {len(msg_ids)} 封")
            total_applied += _batch_modify(service, msg_ids, add_label_ids=[label_id], log=log, control=control)
            if save_as_rule:
                rules = add_rule(rules, "address", addr, label_name, rule_archive)

    if save_as_rule:
        save_rules(rules)
        log(f"已新增 {len(selected_keys)} 筆自動歸類規則，之後同寄件者的新信會自動套用。")

    log(f"完成！共套用 {total_applied} 封信件的標籤。")
    return total_applied


# ---------------------------------------------------------------------------
# 標籤管理（重新命名／刪除／更換）
# ---------------------------------------------------------------------------

def list_user_labels(service) -> list[dict]:
    """回傳使用者自訂標籤清單（不含系統標籤），含信件數，依名稱排序。"""
    result = service.users().labels().list(userId="me").execute(num_retries=3)
    user_labels = [lb for lb in result.get("labels", []) if lb.get("type") == "user"]
    detailed = []
    for lb in user_labels:
        info = service.users().labels().get(userId="me", id=lb["id"]).execute(num_retries=3)
        detailed.append({
            "id": info["id"],
            "name": info["name"],
            "messages_total": info.get("messagesTotal", 0),
        })
    detailed.sort(key=lambda d: d["name"].lower())
    return detailed


def rename_label(service, label_id: str, new_name: str) -> dict:
    """重新命名標籤。注意：巢狀子標籤不會自動跟著改名（Gmail 標籤名稱即路徑）。"""
    safe = sanitize_label(new_name.strip())
    return service.users().labels().patch(
        userId="me", id=label_id, body={"name": safe},
    ).execute(num_retries=3)


def delete_label(service, label_id: str) -> None:
    """刪除標籤（只移除標籤本身，不會刪除信件）。"""
    service.users().labels().delete(userId="me", id=label_id).execute(num_retries=3)


def list_message_ids(service, query: str, max_results: int = 10000,
                     log=print, control: "JobControl | None" = None) -> list[str]:
    """僅列出符合查詢條件的信件 ID（不抓寄件者資訊），用於標籤批次操作。"""
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        if control and control.is_stopped():
            break
        params = {"userId": "me", "q": query, "maxResults": min(500, max_results - len(ids))}
        if page_token:
            params["pageToken"] = page_token
        result = service.users().messages().list(**params).execute(num_retries=3)
        messages = result.get("messages", [])
        ids.extend(m["id"] for m in messages)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return ids


def swap_label_on_messages(service, old_label_id: str, old_label_name: str, new_label_name: str,
                            log=print, control: "JobControl | None" = None) -> int:
    """把目前帶有 old_label 的信件換成 new_label（新增新標籤、移除舊標籤）。"""
    label_cache = list_existing_labels(service)
    safe_new = sanitize_label(new_label_name.strip())
    new_label_id = ensure_label(service, safe_new, label_cache)
    log(f"搜尋標籤「{old_label_name}」的信件...")
    msg_ids = list_message_ids(service, f'label:"{old_label_name}"', log=log, control=control)
    log(f"找到 {len(msg_ids)} 封，換成「{safe_new}」...")
    total = _batch_modify(
        service, msg_ids,
        add_label_ids=[new_label_id], remove_label_ids=[old_label_id],
        log=log, control=control,
    )
    log(f"完成！共 {total} 封信件已從「{old_label_name}」換成「{safe_new}」。")
    return total


# ---------------------------------------------------------------------------
# 自動歸類規則
# ---------------------------------------------------------------------------

def load_rules() -> list[dict]:
    """讀取自動歸類規則：[{match_type, match_value, label, archive}, ...]。"""
    if not os.path.exists(RULES_FILE):
        return []
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_rules(rules: list[dict]) -> None:
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


def add_rule(rules: list[dict], match_type: str, match_value: str, label: str, archive: bool) -> list[dict]:
    """新增規則；若同一個 match_type+match_value 已存在則覆蓋，回傳更新後的規則清單。"""
    match_value = match_value.strip().lower()
    rules = [r for r in rules if not (r["match_type"] == match_type and r["match_value"] == match_value)]
    rules.append({"match_type": match_type, "match_value": match_value, "label": label, "archive": archive})
    return rules


def match_rule(addr: str, domain: str, rules: list[dict]) -> dict | None:
    """依信件地址／網域比對規則，地址完全比對優先於網域比對。"""
    for r in rules:
        if r["match_type"] == "address" and r["match_value"] == addr:
            return r
    for r in rules:
        if r["match_type"] == "domain" and r["match_value"] == domain:
            return r
    return None


# ---------------------------------------------------------------------------
# 收件匣自動歸類（背景監看）
# ---------------------------------------------------------------------------

def get_current_history_id(service) -> str:
    return service.users().getProfile(userId="me").execute(num_retries=3)["historyId"]


def load_watch_state() -> str | None:
    if not os.path.exists(WATCH_STATE_FILE):
        return None
    with open(WATCH_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("history_id")


def save_watch_state(history_id: str) -> None:
    with open(WATCH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"history_id": history_id}, f)


def fetch_new_inbox_message_ids(service, start_history_id: str) -> tuple[str, list[str]]:
    """取得自 start_history_id 之後新增到收件匣的信件 ID，回傳 (最新 history_id, [msg_id, ...])。"""
    ids: list[str] = []
    page_token = None
    latest = start_history_id
    while True:
        params = {
            "userId": "me", "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded"],
        }
        if page_token:
            params["pageToken"] = page_token
        result = service.users().history().list(**params).execute(num_retries=3)
        for record in result.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                if "INBOX" in msg.get("labelIds", []):
                    ids.append(msg["id"])
        latest = result.get("historyId", latest)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return latest, ids


def get_message_sender(service, msg_id: str) -> tuple[str, str, str]:
    """回傳 (display_name, email_addr, domain)。"""
    msg_data = service.users().messages().get(
        userId="me", id=msg_id, format="metadata", metadataHeaders=["From"],
    ).execute(num_retries=3)
    headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
    name, addr = parse_sender(headers.get("From", ""))
    return name, addr, get_domain(addr)


def watch_inbox(get_service, interval_sec: int, log=print, control: "JobControl | None" = None) -> None:
    """背景持續監看收件匣，依 rules.json 自動套用標籤（規則可設定是否同時歸檔）。

    get_service：每次需要連線時呼叫取得 Gmail service 的函式（例如 authenticate）。
    每輪檢查都會重新呼叫一次，避免長時間共用同一個連線物件，遇到底層連線失效時卡死。
    """
    from googleapiclient.errors import HttpError

    service = get_service()
    history_id = load_watch_state() or get_current_history_id(service)
    save_watch_state(history_id)
    label_cache = list_existing_labels(service)
    log(f"自動歸類已啟動（每 {interval_sec} 秒檢查一次新信）")

    while not (control and control.is_stopped()):
        try:
            service = get_service()
            rules = load_rules()
            if rules:
                new_history_id, msg_ids = fetch_new_inbox_message_ids(service, history_id)
                for msg_id in msg_ids:
                    if control and control.is_stopped():
                        break
                    name, addr, domain = get_message_sender(service, msg_id)
                    rule = match_rule(addr, domain, rules)
                    if not rule:
                        continue
                    label_id = ensure_label(service, rule["label"], label_cache)
                    remove = ["INBOX"] if rule.get("archive") else None
                    _batch_modify(service, [msg_id], add_label_ids=[label_id], remove_label_ids=remove)
                    suffix = "（已歸檔）" if rule.get("archive") else ""
                    log(f"自動歸類：{name} <{addr}> → 「{rule['label']}」{suffix}")
                history_id = new_history_id
                save_watch_state(history_id)
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status == 404:
                history_id = get_current_history_id(service)
                save_watch_state(history_id)
                log("同步基準點已過期，已重新校正（僅會處理校正之後的新信）")
            else:
                log(f"自動歸類發生錯誤：{e}")
        except Exception as e:
            log(f"自動歸類發生錯誤：{e}")

        for _ in range(interval_sec):
            if control and control.is_stopped():
                break
            time.sleep(1)

    log("自動歸類已停止。")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def run(query: str = "in:inbox", max_results: int = 200):
    print("登入 Google 帳號...")
    service = authenticate()
    profile = service.users().getProfile(userId="me").execute(num_retries=3)
    print(f"已登入：{profile.get('emailAddress', '')}")

    sender_map = fetch_senders(service, query=query, max_results=max_results)
    if not sender_map:
        print("找不到任何信件。")
        return

    sorted_senders = sorted(sender_map.items(), key=lambda x: -len(x[1]))
    print(f"\n找到 {len(sorted_senders)} 位寄件者：")
    for i, (key, msgs) in enumerate(sorted_senders, 1):
        name, addr, _ = key
        print(f"  {i:>3}. [{len(msgs):>4} 封]  {name}  <{addr}>")

    choice = input("\n請輸入要整理的編號（逗號分隔，all = 全部）：").strip()
    if choice.lower() == "all":
        selected = [k for k, _ in sorted_senders]
    else:
        indices = [int(x) for x in re.split(r"[,，\s]+", choice) if x.strip().isdigit()]
        selected = [sorted_senders[i - 1][0] for i in indices if 1 <= i <= len(sorted_senders)]

    if selected:
        apply_labels_to_senders(service, sender_map, selected)


if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "in:inbox"
    max_r = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    run(query, max_r)
