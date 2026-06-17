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
import os
import re
import threading
from collections import defaultdict

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
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
                  log=print, control: "JobControl | None" = None) -> dict:
    """掃描信件並依寄件者分組。
    回傳 {(display_name, email_addr, domain): [msg_id, ...]}。"""
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

        result = service.users().messages().list(**params).execute()
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
                ).execute()
                headers = {
                    h["name"]: h["value"]
                    for h in msg_data.get("payload", {}).get("headers", [])
                }
                name, addr = parse_sender(headers.get("From", ""))
                domain = get_domain(addr)
                sender_map[(name, addr, domain)].append(msg["id"])
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
    result = service.users().labels().list(userId="me").execute()
    return {lb["name"]: lb["id"] for lb in result.get("labels", [])}


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
    ).execute()
    cache[label_name] = created["id"]
    return created["id"]


# ---------------------------------------------------------------------------
# 套用標籤
# ---------------------------------------------------------------------------

def _batch_apply(service, msg_ids: list[str], label_id: str,
                 log=print, control: "JobControl | None" = None) -> int:
    """把 label_id 批次套用到 msg_ids，每次最多 1000 封，回傳套用數。"""
    applied = 0
    for i in range(0, len(msg_ids), 1000):
        if control:
            control.wait_if_paused()
            if control.is_stopped():
                break
        batch = msg_ids[i:i + 1000]
        service.users().messages().batchModify(
            userId="me",
            body={"ids": batch, "addLabelIds": [label_id]},
        ).execute()
        applied += len(batch)
    return applied


def apply_labels_to_senders(service, sender_msg_map: dict, selected_keys: list,
                             group_label: str = "",
                             log=print, control: "JobControl | None" = None) -> int:
    """對選取的寄件者套用標籤，回傳套用總封數。

    group_label 非空時：所有選取寄件者的信件統一套用同一個自訂標籤。
    group_label 為空時：依「寄件者/網域/名稱」自動建立各自的巢狀標籤。
    """
    label_cache = list_existing_labels(service)
    total_applied = 0

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
        total_applied = _batch_apply(service, all_ids, label_id, log=log, control=control)
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
            total_applied += _batch_apply(service, msg_ids, label_id, log=log, control=control)

    log(f"完成！共套用 {total_applied} 封信件的標籤。")
    return total_applied


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def run(query: str = "in:inbox", max_results: int = 200):
    print("登入 Google 帳號...")
    service = authenticate()
    profile = service.users().getProfile(userId="me").execute()
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
