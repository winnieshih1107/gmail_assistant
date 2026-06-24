"""
Gmail 整理小助手 — 桌面操作介面 (tkinter)
分頁：
  1. 整理信件：登入 → 分析寄件者 → 套用標籤（可同時存成自動歸類規則）
  2. 標籤管理：重新命名／刪除標籤，或把信件從某個標籤換成另一個
  3. 自動歸類：維護規則清單，啟動背景監看後新信會自動套用標籤
"""

import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, simpledialog, ttk

from gmail_organizer import (
    authenticate,
    fetch_senders,
    apply_labels_to_senders,
    list_user_labels,
    list_label_id_name_map,
    rename_label,
    delete_label,
    swap_label_on_messages,
    load_rules,
    save_rules,
    add_rule,
    watch_inbox,
    JobControl,
    LABEL_PREFIX,
)

MATCH_TYPE_LABELS = {"address": "寄件者地址", "domain": "寄件者網域"}
MATCH_TYPE_BY_LABEL = {v: k for k, v in MATCH_TYPE_LABELS.items()}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Gmail 整理小助手")
        root.geometry("900x780")

        self.log_queue = __import__("queue").Queue()
        self.service = None

        # 整理信件分頁狀態
        self.worker_running = False
        self.control = JobControl()
        self.sender_map: dict = {}
        self.sender_current_labels: dict = {}
        self.sender_checkboxes: list[tuple[tk.BooleanVar, tuple]] = []

        # 標籤管理分頁狀態
        self.label_control = JobControl()
        self.labels_cache: list[dict] = []

        # 自動歸類分頁狀態
        self.rules: list[dict] = load_rules()
        self.watch_control = JobControl()
        self.watch_running = False

        self._build_ui()
        self._refresh_rules_tree()
        self.root.after(150, self._poll_log)

    # ── UI 建構（共用框架） ───────────────────────────────────────

    def _build_ui(self):
        # 認證區（所有分頁共用）
        auth_frame = tk.LabelFrame(self.root, text="Gmail 帳號", padx=8, pady=6)
        auth_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.auth_label = tk.Label(auth_frame, text="尚未登入", fg="gray")
        self.auth_label.pack(side="left")
        self.auth_btn = tk.Button(auth_frame, text="登入 Google 帳號", command=self.on_auth)
        self.auth_btn.pack(side="right")

        self.status_label = tk.Label(self.root, text="請先登入 Google 帳號", fg="gray", anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(4, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=False, padx=10, pady=(8, 0))

        tab_organize = tk.Frame(self.notebook)
        tab_labels = tk.Frame(self.notebook)
        tab_watch = tk.Frame(self.notebook)
        self.notebook.add(tab_organize, text="整理信件")
        self.notebook.add(tab_labels, text="標籤管理")
        self.notebook.add(tab_watch, text="自動歸類")

        self._build_organize_tab(tab_organize)
        self._build_labels_tab(tab_labels)
        self._build_watch_tab(tab_watch)

        # 進度／log 區（所有分頁共用）
        mid = tk.Frame(self.root, padx=10, pady=6)
        mid.pack(fill="both", expand=True)
        tk.Label(mid, text="進度：").pack(anchor="w")
        self.output = scrolledtext.ScrolledText(mid, wrap="word", height=10, font=("Microsoft JhengHei", 10))
        self.output.pack(fill="both", expand=True)
        self.output.tag_configure("ok", foreground="#1a7a3c")
        self.output.tag_configure("err", foreground="#c0392b")
        self.output.tag_configure("dim", foreground="#888888", font=("Microsoft JhengHei", 9, "italic"))

    # ── 分頁 1：整理信件 ──────────────────────────────────────────

    def _build_organize_tab(self, parent):
        scan_frame = tk.LabelFrame(parent, text="掃描條件", padx=8, pady=6)
        scan_frame.pack(fill="x", padx=4, pady=(8, 0))

        r1 = tk.Frame(scan_frame)
        r1.pack(fill="x")
        tk.Label(r1, text="Gmail 搜尋語法：").pack(side="left")
        self.query_entry = tk.Entry(r1, font=("Microsoft JhengHei", 11))
        self.query_entry.insert(0, "in:inbox")
        self.query_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.query_entry.bind("<Return>", lambda e: self.on_scan())

        r2 = tk.Frame(scan_frame)
        r2.pack(fill="x", pady=(4, 0))
        tk.Label(r2, text="最多掃描封數：").pack(side="left")
        self.limit_var = tk.StringVar(value="200")
        tk.Spinbox(r2, from_=50, to=5000, increment=50,
                   textvariable=self.limit_var, width=8).pack(side="left", padx=(4, 12))
        tk.Label(r2, text="常用語法：in:inbox　in:all　from:@gmail.com　is:unread",
                 fg="gray").pack(side="left")
        self.scan_btn = tk.Button(r2, text="分析寄件者", command=self.on_scan, state="disabled")
        self.scan_btn.pack(side="right")

        r3 = tk.Frame(scan_frame)
        r3.pack(fill="x", pady=(4, 0))
        tk.Label(r3, text="查詢已建立的標籤：").pack(side="left")
        self.query_label_var = tk.StringVar()
        self.query_label_combo = ttk.Combobox(
            r3, textvariable=self.query_label_var, width=36, state="readonly",
        )
        self.query_label_combo.pack(side="left", padx=(4, 8))
        tk.Button(r3, text="套用到查詢", command=self.on_use_label_query).pack(side="left")
        tk.Button(r3, text="重新整理標籤", command=self.on_refresh_labels).pack(side="left", padx=(4, 0))

        list_frame = tk.LabelFrame(parent, text="寄件者清單（勾選要整理的寄件者）", padx=8, pady=6)
        list_frame.pack(fill="both", expand=False, padx=4, pady=(8, 0))

        tb = tk.Frame(list_frame)
        tb.pack(fill="x")
        tk.Button(tb, text="全選", command=lambda: self._set_all(True)).pack(side="left")
        tk.Button(tb, text="全不選", command=lambda: self._set_all(False)).pack(side="left", padx=(6, 0))
        tk.Label(tb, text="排序：").pack(side="left", padx=(14, 0))
        self.sort_var = tk.StringVar(value="count")
        ttk.Combobox(tb, textvariable=self.sort_var, width=14, state="readonly",
                     values=["信件數（多→少）", "寄件者名稱 A→Z"]).pack(side="left", padx=(4, 0))
        tk.Button(tb, text="重新排序", command=self._repopulate).pack(side="left", padx=(4, 0))
        self.apply_btn = tk.Button(
            tb, text="套用標籤", command=self.on_apply, state="disabled",
            bg="#4285F4", fg="white", font=("Microsoft JhengHei", 10, "bold"),
        )
        self.apply_btn.pack(side="right")

        gb = tk.Frame(list_frame)
        gb.pack(fill="x", pady=(4, 0))
        tk.Label(gb, text="群組標籤（選填）：").pack(side="left")
        self.group_entry = tk.Entry(gb, font=("Microsoft JhengHei", 10), width=28)
        self.group_entry.pack(side="left", padx=(4, 0))
        tk.Label(
            gb,
            text="填入後，勾選的所有寄件者信件將歸入同一個標籤；留空則各自分類",
            fg="gray",
        ).pack(side="left", padx=(8, 0))

        rb = tk.Frame(list_frame)
        rb.pack(fill="x", pady=(4, 0))
        self.save_rule_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            rb, text="同時加入自動歸類規則（之後同寄件者的新信會自動套用同樣標籤）",
            variable=self.save_rule_var,
        ).pack(side="left")
        self.rule_archive_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            rb, text="自動歸類時同時歸檔（移出收件匣）", variable=self.rule_archive_var,
        ).pack(side="left", padx=(12, 0))

        jb = tk.Frame(list_frame)
        jb.pack(fill="x", pady=(4, 0))
        self.pause_btn = tk.Button(jb, text="暫停", command=self.on_pause_resume, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = tk.Button(jb, text="停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        tk.Label(jb, text=f"留空標籤結構：{LABEL_PREFIX} / 網域 / 寄件者名稱", fg="gray").pack(side="right")

        cf = tk.Frame(list_frame)
        cf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cf, height=180, highlightthickness=0)
        sb = tk.Scrollbar(cf, orient="vertical", command=self.canvas.yview)
        self.checklist_frame = tk.Frame(self.canvas)
        self.checklist_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.checklist_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    # ── 分頁 2：標籤管理 ──────────────────────────────────────────

    def _build_labels_tab(self, parent):
        top = tk.Frame(parent, padx=4, pady=8)
        top.pack(fill="x")
        tk.Button(top, text="重新整理標籤列表", command=self.on_refresh_labels).pack(side="left")
        tk.Button(top, text="重新命名", command=self.on_rename_label).pack(side="left", padx=(8, 0))
        tk.Button(top, text="刪除標籤", command=self.on_delete_label, fg="#c0392b").pack(side="left", padx=(4, 0))

        tree_frame = tk.Frame(parent, padx=4)
        tree_frame.pack(fill="both", expand=True)
        self.labels_tree = ttk.Treeview(
            tree_frame, columns=("name", "count"), show="headings", height=12,
        )
        self.labels_tree.heading("name", text="標籤名稱")
        self.labels_tree.heading("count", text="信件數")
        self.labels_tree.column("name", width=520, anchor="w")
        self.labels_tree.column("count", width=80, anchor="center")
        tsb = tk.Scrollbar(tree_frame, orient="vertical", command=self.labels_tree.yview)
        self.labels_tree.configure(yscrollcommand=tsb.set)
        self.labels_tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")

        swap_frame = tk.LabelFrame(parent, text="更換標籤（把信件從某個標籤換成另一個）", padx=8, pady=6)
        swap_frame.pack(fill="x", padx=4, pady=(8, 4))
        tk.Label(swap_frame, text="來源標籤：").grid(row=0, column=0, sticky="w")
        self.swap_source_var = tk.StringVar()
        self.swap_source_combo = ttk.Combobox(
            swap_frame, textvariable=self.swap_source_var, width=36, state="readonly",
        )
        self.swap_source_combo.grid(row=0, column=1, sticky="w", padx=(4, 12))
        tk.Label(swap_frame, text="新標籤名稱：").grid(row=0, column=2, sticky="w")
        self.swap_target_entry = tk.Entry(swap_frame, width=30)
        self.swap_target_entry.grid(row=0, column=3, sticky="w", padx=(4, 12))
        self.swap_btn = tk.Button(
            swap_frame, text="套用新標籤並移除舊標籤", command=self.on_swap_label,
        )
        self.swap_btn.grid(row=0, column=4, sticky="e")

    # ── 分頁 3：自動歸類 ──────────────────────────────────────────

    def _build_watch_tab(self, parent):
        rules_frame = tk.LabelFrame(parent, text="自動歸類規則", padx=8, pady=6)
        rules_frame.pack(fill="both", expand=True, padx=4, pady=(8, 0))

        self.rules_tree = ttk.Treeview(
            rules_frame, columns=("type", "value", "label", "archive"),
            show="headings", height=8,
        )
        self.rules_tree.heading("type", text="比對方式")
        self.rules_tree.heading("value", text="比對值")
        self.rules_tree.heading("label", text="套用標籤")
        self.rules_tree.heading("archive", text="同時歸檔")
        self.rules_tree.column("type", width=90, anchor="w")
        self.rules_tree.column("value", width=200, anchor="w")
        self.rules_tree.column("label", width=280, anchor="w")
        self.rules_tree.column("archive", width=70, anchor="center")
        self.rules_tree.pack(fill="both", expand=True)

        add_frame = tk.Frame(rules_frame)
        add_frame.pack(fill="x", pady=(6, 0))
        tk.Label(add_frame, text="比對方式：").grid(row=0, column=0, sticky="w")
        self.rule_type_var = tk.StringVar(value=MATCH_TYPE_LABELS["address"])
        ttk.Combobox(
            add_frame, textvariable=self.rule_type_var, width=12, state="readonly",
            values=list(MATCH_TYPE_LABELS.values()),
        ).grid(row=0, column=1, sticky="w", padx=(4, 12))
        tk.Label(add_frame, text="比對值：").grid(row=0, column=2, sticky="w")
        self.rule_value_entry = tk.Entry(add_frame, width=24)
        self.rule_value_entry.grid(row=0, column=3, sticky="w", padx=(4, 12))
        tk.Label(add_frame, text="套用標籤：").grid(row=0, column=4, sticky="w")
        self.rule_label_entry = tk.Entry(add_frame, width=24)
        self.rule_label_entry.grid(row=0, column=5, sticky="w", padx=(4, 12))
        self.rule_archive_add_var = tk.BooleanVar(value=False)
        tk.Checkbutton(add_frame, text="歸檔", variable=self.rule_archive_add_var).grid(row=0, column=6, sticky="w")

        btn_frame = tk.Frame(rules_frame)
        btn_frame.pack(fill="x", pady=(6, 0))
        tk.Button(btn_frame, text="新增規則", command=self.on_add_rule).pack(side="left")
        tk.Button(btn_frame, text="刪除選取規則", command=self.on_delete_rule, fg="#c0392b").pack(side="left", padx=(6, 0))

        watch_frame = tk.LabelFrame(parent, text="背景監看", padx=8, pady=6)
        watch_frame.pack(fill="x", padx=4, pady=(8, 4))
        tk.Label(watch_frame, text="檢查間隔（分鐘）：").pack(side="left")
        self.watch_interval_var = tk.StringVar(value="5")
        tk.Spinbox(watch_frame, from_=1, to=60, textvariable=self.watch_interval_var, width=6).pack(side="left", padx=(4, 12))
        self.watch_start_btn = tk.Button(watch_frame, text="啟動自動歸類", command=self.on_watch_start)
        self.watch_start_btn.pack(side="left")
        self.watch_stop_btn = tk.Button(watch_frame, text="停止", command=self.on_watch_stop, state="disabled")
        self.watch_stop_btn.pack(side="left", padx=(6, 0))
        self.watch_status_label = tk.Label(watch_frame, text="未啟動", fg="gray")
        self.watch_status_label.pack(side="left", padx=(12, 0))

    # ── 共用工具 ───────────────────────────────────────────────────

    def log(self, msg: str, tag: str = ""):
        self.log_queue.put((msg, tag))

    def _poll_log(self):
        import queue
        try:
            while True:
                msg, tag = self.log_queue.get_nowait()
                self.output.insert("end", msg + "\n", tag)
                self.output.see("end")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log)

    def set_status(self, text: str, color: str = "gray"):
        self.status_label.config(text=text, fg=color)

    def _set_job_btns(self, enabled: bool):
        s = "normal" if enabled else "disabled"
        self.pause_btn.config(state=s, text="暫停")
        self.stop_btn.config(state=s)

    def on_pause_resume(self):
        if self.control.is_paused():
            self.control.request_resume()
            self.pause_btn.config(text="暫停")
            self.set_status("已繼續...", "blue")
        else:
            self.control.request_pause()
            self.pause_btn.config(text="繼續")
            self.set_status("已暫停，按「繼續」恢復", "orange")

    def on_stop(self):
        self.control.request_stop()
        self._set_job_btns(False)
        self.set_status("正在停止...", "orange")

    def _set_all(self, value: bool):
        for var, _ in self.sender_checkboxes:
            var.set(value)

    def _clear_checklist(self):
        for w in self.checklist_frame.winfo_children():
            w.destroy()
        self.sender_checkboxes = []
        self.apply_btn.config(state="disabled")

    def _populate(self, keys: list):
        self._clear_checklist()
        for key in keys:
            name, addr, domain = key
            count = len(self.sender_map[key])
            var = tk.BooleanVar(value=False)
            current_labels = self.sender_current_labels.get(key) or []
            classified = f"  →  已歸類：{'、'.join(current_labels)}" if current_labels else ""
            label = f"[{count:>4} 封]  {name}  <{addr}>  [{domain}]{classified}"
            tk.Checkbutton(
                self.checklist_frame, text=label, variable=var,
                anchor="w", justify="left", wraplength=800,
                font=("Microsoft JhengHei", 10),
            ).pack(fill="x", anchor="w")
            self.sender_checkboxes.append((var, key))
        if keys:
            self.apply_btn.config(state="normal")

    def _sorted_keys(self) -> list:
        if "名稱" in self.sort_var.get():
            return sorted(self.sender_map, key=lambda k: k[0].lower())
        return sorted(self.sender_map, key=lambda k: -len(self.sender_map[k]))

    def _repopulate(self):
        if self.sender_map:
            self._populate(self._sorted_keys())

    # ── 認證 ───────────────────────────────────────────────────────

    def on_auth(self):
        self.auth_btn.config(state="disabled", text="登入中...")
        threading.Thread(target=self._auth_worker, daemon=True).start()

    def _auth_worker(self):
        try:
            svc = authenticate()
            profile = svc.users().getProfile(userId="me").execute()
            self.service = svc
            addr = profile.get("emailAddress", "")
            self.root.after(0, lambda: self._on_auth_ok(addr))
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._on_auth_err(msg))

    def _on_auth_ok(self, addr: str):
        self.auth_label.config(text=f"已登入：{addr}", fg="green")
        self.auth_btn.config(text="重新登入", state="normal")
        self.scan_btn.config(state="normal")
        self.set_status("登入成功，設定條件後按「分析寄件者」", "green")
        self.log(f"已登入：{addr}", "ok")
        self.on_refresh_labels()

    def _on_auth_err(self, msg: str):
        self.auth_label.config(text="登入失敗", fg="red")
        self.auth_btn.config(text="登入 Google 帳號", state="normal")
        self.set_status("登入失敗", "red")
        self.log(f"登入失敗：{msg}", "err")
        messagebox.showerror("登入失敗", msg)

    # ── 分析寄件者 ─────────────────────────────────────────────────

    def on_scan(self):
        if self.worker_running or not self.service:
            return
        query = self.query_entry.get().strip() or "in:inbox"
        try:
            max_r = int(self.limit_var.get())
        except ValueError:
            max_r = 200

        self._clear_checklist()
        self.worker_running = True
        self.control.reset()
        self.scan_btn.config(state="disabled", text="掃描中...")
        self._set_job_btns(True)
        self.set_status("掃描信件中，請稍候...", "blue")
        threading.Thread(target=self._scan_worker, args=(query, max_r), daemon=True).start()

    def _scan_worker(self, query: str, max_r: int):
        try:
            svc = authenticate()
            label_ids_by_sender: dict = {}
            result = fetch_senders(
                svc, query=query, max_results=max_r,
                log=lambda m: self.log(m, "dim"), control=self.control,
                label_ids_out=label_ids_by_sender,
            )
            label_map = list_label_id_name_map(svc)
            self.sender_map = result
            self.sender_current_labels = {
                key: sorted(label_map[lid] for lid in ids if lid in label_map)
                for key, ids in label_ids_by_sender.items()
            }
            self.root.after(0, lambda: self._on_scan_ok())
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._on_scan_err(msg))

    def _on_scan_ok(self):
        self.worker_running = False
        self.scan_btn.config(state="normal", text="分析寄件者")
        self._set_job_btns(False)
        if not self.sender_map:
            self.set_status("找不到任何信件", "orange")
            return
        self.set_status(f"找到 {len(self.sender_map)} 位寄件者，請勾選後按「套用標籤」", "green")
        self._populate(self._sorted_keys())

    def _on_scan_err(self, msg: str):
        self.worker_running = False
        self.scan_btn.config(state="normal", text="分析寄件者")
        self._set_job_btns(False)
        self.set_status("掃描失敗", "red")
        self.log(f"掃描失敗：{msg}", "err")
        messagebox.showerror("掃描失敗", msg)

    # ── 套用標籤 ───────────────────────────────────────────────────

    def on_apply(self):
        if self.worker_running or not self.service:
            return
        selected = [key for var, key in self.sender_checkboxes if var.get()]
        if not selected:
            messagebox.showwarning("提示", "請至少勾選一位寄件者")
            return
        group_label = self.group_entry.get().strip()
        total_mails = sum(len(self.sender_map[k]) for k in selected)
        if group_label:
            confirm_msg = (
                f"將把 {len(selected)} 位寄件者（共 {total_mails} 封信）\n"
                f"統一歸入標籤「{group_label}」，確定繼續？"
            )
        else:
            confirm_msg = (
                f"將對 {len(selected)} 位寄件者（共 {total_mails} 封信）\n"
                f"各自建立「{LABEL_PREFIX}/網域/名稱」標籤並套用，確定繼續？"
            )
        if not messagebox.askyesno("確認套用", confirm_msg):
            return

        self.worker_running = True
        self.control.reset()
        self.apply_btn.config(state="disabled", text="套用中...")
        self._set_job_btns(True)
        self.set_status(f"套用標籤中（{len(selected)} 位寄件者）...", "blue")
        save_rule = self.save_rule_var.get()
        rule_archive = self.rule_archive_var.get()
        threading.Thread(
            target=self._apply_worker, args=(selected, group_label, save_rule, rule_archive), daemon=True,
        ).start()

    def _apply_worker(self, selected: list, group_label: str, save_rule: bool, rule_archive: bool):
        try:
            svc = authenticate()
            total = apply_labels_to_senders(
                svc, self.sender_map, selected,
                group_label=group_label,
                log=lambda m: self.log(m, "ok"), control=self.control,
                save_as_rule=save_rule, rule_archive=rule_archive,
            )
            self.root.after(0, lambda: self._on_apply_ok(total))
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._on_apply_err(msg))

    def _on_apply_ok(self, total: int):
        self.worker_running = False
        self.apply_btn.config(state="normal", text="套用標籤")
        self._set_job_btns(False)
        self.set_status(f"完成！已套用 {total} 封信件的標籤", "green")
        if self.save_rule_var.get():
            self.rules = load_rules()
            self._refresh_rules_tree()

    def _on_apply_err(self, msg: str):
        self.worker_running = False
        self.apply_btn.config(state="normal", text="套用標籤")
        self._set_job_btns(False)
        self.set_status("套用失敗", "red")
        self.log(f"套用失敗：{msg}", "err")
        messagebox.showerror("套用失敗", msg)

    # ── 標籤管理 ───────────────────────────────────────────────────

    def on_refresh_labels(self):
        if not self.service:
            messagebox.showwarning("提示", "請先登入 Google 帳號")
            return
        threading.Thread(target=self._refresh_labels_worker, daemon=True).start()

    def _refresh_labels_worker(self):
        try:
            svc = authenticate()
            labels = list_user_labels(svc)
            self.root.after(0, lambda: self._on_labels_loaded(labels))
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self.log(f"讀取標籤失敗：{msg}", "err"))

    def _on_labels_loaded(self, labels: list[dict]):
        self.labels_cache = labels
        self.labels_tree.delete(*self.labels_tree.get_children())
        for lb in labels:
            self.labels_tree.insert("", "end", iid=lb["id"], values=(lb["name"], lb["messages_total"]))
        names = [lb["name"] for lb in labels]
        self.swap_source_combo["values"] = names
        self.query_label_combo["values"] = names
        self.log(f"已讀取 {len(labels)} 個自訂標籤", "dim")

    def on_use_label_query(self):
        name = self.query_label_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "請先選擇一個標籤")
            return
        self.query_entry.delete(0, "end")
        self.query_entry.insert(0, f'label:"{name}"')

    def _selected_label(self) -> dict | None:
        sel = self.labels_tree.selection()
        if not sel:
            return None
        label_id = sel[0]
        return next((lb for lb in self.labels_cache if lb["id"] == label_id), None)

    def on_rename_label(self):
        lb = self._selected_label()
        if not lb:
            messagebox.showwarning("提示", "請先在列表中選取一個標籤")
            return
        new_name = simpledialog.askstring("重新命名標籤", f"將「{lb['name']}」改名為：", initialvalue=lb["name"])
        if not new_name or new_name.strip() == lb["name"]:
            return
        try:
            rename_label(authenticate(), lb["id"], new_name)
            self.log(f"已將標籤「{lb['name']}」改名為「{new_name.strip()}」", "ok")
            self.on_refresh_labels()
        except Exception as e:
            self.log(f"重新命名失敗：{e}", "err")
            messagebox.showerror("重新命名失敗", str(e))

    def on_delete_label(self):
        lb = self._selected_label()
        if not lb:
            messagebox.showwarning("提示", "請先在列表中選取一個標籤")
            return
        if not messagebox.askyesno("確認刪除", f"確定要刪除標籤「{lb['name']}」嗎？\n（只移除標籤，不會刪除信件）"):
            return
        try:
            delete_label(authenticate(), lb["id"])
            self.log(f"已刪除標籤「{lb['name']}」", "ok")
            self.on_refresh_labels()
        except Exception as e:
            self.log(f"刪除失敗：{e}", "err")
            messagebox.showerror("刪除失敗", str(e))

    def on_swap_label(self):
        source_name = self.swap_source_var.get().strip()
        target_name = self.swap_target_entry.get().strip()
        if not source_name or not target_name:
            messagebox.showwarning("提示", "請選擇來源標籤並輸入新標籤名稱")
            return
        source_lb = next((lb for lb in self.labels_cache if lb["name"] == source_name), None)
        if not source_lb:
            messagebox.showwarning("提示", "找不到來源標籤，請重新整理標籤列表")
            return
        if not messagebox.askyesno(
            "確認更換標籤",
            f"將把目前帶有「{source_name}」標籤的所有信件\n換成「{target_name}」，確定繼續？",
        ):
            return
        self.swap_btn.config(state="disabled", text="處理中...")
        self.label_control.reset()
        threading.Thread(
            target=self._swap_worker, args=(source_lb["id"], source_name, target_name), daemon=True,
        ).start()

    def _swap_worker(self, source_id: str, source_name: str, target_name: str):
        try:
            svc = authenticate()
            swap_label_on_messages(
                svc, source_id, source_name, target_name,
                log=lambda m: self.log(m, "ok"), control=self.label_control,
            )
            self.root.after(0, self.on_refresh_labels)
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self.log(f"更換標籤失敗：{msg}", "err"))
        finally:
            self.root.after(0, lambda: self.swap_btn.config(state="normal", text="套用新標籤並移除舊標籤"))

    # ── 自動歸類規則 ───────────────────────────────────────────────

    def _refresh_rules_tree(self):
        self.rules_tree.delete(*self.rules_tree.get_children())
        for i, r in enumerate(self.rules):
            self.rules_tree.insert("", "end", iid=str(i), values=(
                MATCH_TYPE_LABELS.get(r["match_type"], r["match_type"]),
                r["match_value"],
                r["label"],
                "是" if r.get("archive") else "否",
            ))

    def on_add_rule(self):
        match_type = MATCH_TYPE_BY_LABEL.get(self.rule_type_var.get(), "address")
        value = self.rule_value_entry.get().strip()
        label = self.rule_label_entry.get().strip()
        if not value or not label:
            messagebox.showwarning("提示", "請輸入比對值與套用標籤")
            return
        self.rules = add_rule(self.rules, match_type, value, label, self.rule_archive_add_var.get())
        save_rules(self.rules)
        self._refresh_rules_tree()
        self.rule_value_entry.delete(0, "end")
        self.rule_label_entry.delete(0, "end")
        self.log(f"已新增規則：{MATCH_TYPE_LABELS[match_type]}「{value}」→「{label}」", "ok")

    def on_delete_rule(self):
        sel = self.rules_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "請先選取要刪除的規則")
            return
        indices = sorted((int(i) for i in sel), reverse=True)
        for i in indices:
            del self.rules[i]
        save_rules(self.rules)
        self._refresh_rules_tree()
        self.log("已刪除選取的規則", "ok")

    # ── 背景監看 ───────────────────────────────────────────────────

    def on_watch_start(self):
        if not self.service:
            messagebox.showwarning("提示", "請先登入 Google 帳號")
            return
        if self.watch_running:
            return
        try:
            minutes = float(self.watch_interval_var.get())
        except ValueError:
            minutes = 5
        interval_sec = max(30, int(minutes * 60))
        self.watch_running = True
        self.watch_control.reset()
        self.watch_start_btn.config(state="disabled")
        self.watch_stop_btn.config(state="normal")
        self.watch_status_label.config(text="監看中...", fg="blue")
        threading.Thread(target=self._watch_worker, args=(interval_sec,), daemon=True).start()

    def _watch_worker(self, interval_sec: int):
        try:
            watch_inbox(authenticate, interval_sec, log=lambda m: self.log(m, "ok"), control=self.watch_control)
        except Exception as e:
            self.log(f"自動歸類發生錯誤：{e}", "err")
        self.root.after(0, self._on_watch_stopped)

    def on_watch_stop(self):
        self.watch_control.request_stop()
        self.watch_status_label.config(text="正在停止...", fg="orange")

    def _on_watch_stopped(self):
        self.watch_running = False
        self.watch_start_btn.config(state="normal")
        self.watch_stop_btn.config(state="disabled")
        self.watch_status_label.config(text="已停止", fg="gray")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
