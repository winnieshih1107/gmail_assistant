"""
Gmail 整理小助手 — 桌面操作介面 (tkinter)
1. 登入 Google 帳號（OAuth2）
2. 設定掃描條件（Gmail 搜尋語法）→ 分析寄件者
3. 勾選要整理的寄件者 → 套用標籤（寄件者/網域/名稱）
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

from gmail_organizer import (
    authenticate,
    fetch_senders,
    apply_labels_to_senders,
    JobControl,
    LABEL_PREFIX,
)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Gmail 整理小助手")
        root.geometry("860x720")

        self.log_queue: queue.Queue = queue.Queue()
        self.worker_running = False
        self.control = JobControl()
        self.service = None
        self.sender_map: dict = {}
        self.sender_checkboxes: list[tuple[tk.BooleanVar, tuple]] = []

        self._build_ui()
        self.root.after(150, self._poll_log)

    # ── UI 建構 ────────────────────────────────────────────────────

    def _build_ui(self):
        # 認證區
        auth_frame = tk.LabelFrame(self.root, text="Gmail 帳號", padx=8, pady=6)
        auth_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.auth_label = tk.Label(auth_frame, text="尚未登入", fg="gray")
        self.auth_label.pack(side="left")
        self.auth_btn = tk.Button(auth_frame, text="登入 Google 帳號", command=self.on_auth)
        self.auth_btn.pack(side="right")

        # 掃描條件區
        scan_frame = tk.LabelFrame(self.root, text="掃描條件", padx=8, pady=6)
        scan_frame.pack(fill="x", padx=10, pady=(8, 0))

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

        # 狀態列
        self.status_label = tk.Label(self.root, text="請先登入 Google 帳號", fg="gray", anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(4, 0))

        # 寄件者清單
        list_frame = tk.LabelFrame(self.root, text="寄件者清單（勾選要整理的寄件者）", padx=8, pady=6)
        list_frame.pack(fill="both", expand=False, padx=10, pady=(8, 0))

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

        # 群組標籤輸入列
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

        jb = tk.Frame(list_frame)
        jb.pack(fill="x", pady=(4, 0))
        self.pause_btn = tk.Button(jb, text="暫停", command=self.on_pause_resume, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = tk.Button(jb, text="停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        tk.Label(jb, text=f"留空標籤結構：{LABEL_PREFIX} / 網域 / 寄件者名稱", fg="gray").pack(side="right")

        cf = tk.Frame(list_frame)
        cf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cf, height=210, highlightthickness=0)
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

        # 進度區
        mid = tk.Frame(self.root, padx=10, pady=6)
        mid.pack(fill="both", expand=True)
        tk.Label(mid, text="進度：").pack(anchor="w")
        self.output = scrolledtext.ScrolledText(mid, wrap="word", font=("Microsoft JhengHei", 10))
        self.output.pack(fill="both", expand=True)
        self.output.tag_configure("ok", foreground="#1a7a3c")
        self.output.tag_configure("err", foreground="#c0392b")
        self.output.tag_configure("dim", foreground="#888888", font=("Microsoft JhengHei", 9, "italic"))

    # ── 共用工具 ───────────────────────────────────────────────────

    def log(self, msg: str, tag: str = ""):
        self.log_queue.put((msg, tag))

    def _poll_log(self):
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
            label = f"[{count:>4} 封]  {name}  <{addr}>  [{domain}]"
            tk.Checkbutton(
                self.checklist_frame, text=label, variable=var,
                anchor="w", justify="left", wraplength=760,
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
            self.root.after(0, lambda: self._on_auth_err(str(e)))

    def _on_auth_ok(self, addr: str):
        self.auth_label.config(text=f"已登入：{addr}", fg="green")
        self.auth_btn.config(text="重新登入", state="normal")
        self.scan_btn.config(state="normal")
        self.set_status("登入成功，設定條件後按「分析寄件者」", "green")
        self.log(f"已登入：{addr}", "ok")

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
            result = fetch_senders(
                self.service, query=query, max_results=max_r,
                log=lambda m: self.log(m, "dim"), control=self.control,
            )
            self.sender_map = result
            self.root.after(0, lambda: self._on_scan_ok())
        except Exception as e:
            self.root.after(0, lambda: self._on_scan_err(str(e)))

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
        threading.Thread(target=self._apply_worker, args=(selected, group_label), daemon=True).start()

    def _apply_worker(self, selected: list, group_label: str):
        try:
            total = apply_labels_to_senders(
                self.service, self.sender_map, selected,
                group_label=group_label,
                log=lambda m: self.log(m, "ok"), control=self.control,
            )
            self.root.after(0, lambda: self._on_apply_ok(total))
        except Exception as e:
            self.root.after(0, lambda: self._on_apply_err(str(e)))

    def _on_apply_ok(self, total: int):
        self.worker_running = False
        self.apply_btn.config(state="normal", text="套用標籤")
        self._set_job_btns(False)
        self.set_status(f"完成！已套用 {total} 封信件的標籤", "green")

    def _on_apply_err(self, msg: str):
        self.worker_running = False
        self.apply_btn.config(state="normal", text="套用標籤")
        self._set_job_btns(False)
        self.set_status("套用失敗", "red")
        self.log(f"套用失敗：{msg}", "err")
        messagebox.showerror("套用失敗", msg)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
