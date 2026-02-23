"""Main GUI application for AI Rename & Sort."""
import os
import queue
import re
import shutil
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .ai_client import LMStudioClient
from .config_manager import ConfigManager
from .file_processor import FileProcessor
from .watcher import FileWatcher


class AIRenameSortApp:
    """Tkinter GUI application that watches a folder and uses AI to rename/sort files."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AI Rename & Sort")
        self.root.geometry("950x680")
        self.root.minsize(750, 520)

        # Core components
        self.config = ConfigManager()
        self.ai_client = LMStudioClient(self.config.get("lmstudio_url", "http://localhost:1234"))
        self.file_processor = FileProcessor()
        self.watcher: FileWatcher | None = None

        # Processing queue (filepaths to analyse)
        self._proc_queue: queue.Queue[str] = queue.Queue()
        # Maps filepath -> treeview item id
        self._queue_items: dict[str, str] = {}

        self._build_ui()

        # Background processing thread
        threading.Thread(target=self._process_loop, daemon=True).start()

        # Check LMStudio connection shortly after startup
        self.root.after(600, self._check_connection)
        # Periodic model-save check
        self.root.after(2000, self._periodic_save_model)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Tk variables – initialise before any widget references them
        self.watch_folder_var = tk.StringVar(value=self.config.get("watch_folder", ""))
        self.output_folder_var = tk.StringVar(value=self.config.get("output_folder", ""))
        self.status_var = tk.StringVar(value="Stopped")
        self.conn_var = tk.StringVar(value="Not Connected")
        self.auto_apply_var = tk.BooleanVar(value=self.config.get("auto_apply", False))
        self.auto_process_var = tk.BooleanVar(value=self.config.get("auto_process", False))
        self.url_var = tk.StringVar(value=self.config.get("lmstudio_url", "http://localhost:1234"))
        self.model_var = tk.StringVar(value=self.config.get("model", ""))
        self.new_folder_var = tk.StringVar()
        self.settings_status_var = tk.StringVar()

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_top_section(main)

        nb = ttk.Notebook(main)
        nb.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        for title, builder in [
            ("Queue", self._build_queue_tab),
            ("Settings", self._build_settings_tab),
            ("Folders", self._build_folders_tab),
            ("Log", self._build_log_tab),
        ]:
            frame = ttk.Frame(nb)
            nb.add(frame, text=f"  {title}  ")
            builder(frame)

    def _build_top_section(self, parent):
        top = ttk.LabelFrame(parent, text="File Watcher", padding=8)
        top.pack(fill=tk.X)

        for label, var, cmd in [
            ("Watch Folder:", self.watch_folder_var, self._browse_watch),
            ("Output Folder:", self.output_folder_var, self._browse_output),
        ]:
            row = ttk.Frame(top)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
            ttk.Button(row, text="Browse…", command=cmd).pack(side=tk.LEFT)

        ctrl = ttk.Frame(top)
        ctrl.pack(fill=tk.X, pady=(6, 0))

        self.start_btn = ttk.Button(ctrl, text="▶  Start Watching", command=self._start_watching)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.stop_btn = ttk.Button(ctrl, text="■  Stop", command=self._stop_watching, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(ctrl, text="Status:").pack(side=tk.LEFT, padx=(4, 2))
        self.status_label = ttk.Label(ctrl, textvariable=self.status_var, foreground="red", font=("", 9, "bold"))
        self.status_label.pack(side=tk.LEFT)

        ttk.Label(ctrl, text="   LMStudio:").pack(side=tk.LEFT, padx=(8, 2))
        self.conn_label = ttk.Label(ctrl, textvariable=self.conn_var, foreground="red", font=("", 9, "bold"))
        self.conn_label.pack(side=tk.LEFT)

    # ---- Queue tab ---------------------------------------------------

    def _build_queue_tab(self, parent):
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill=tk.X, padx=6, pady=6)

        ttk.Button(ctrl, text="Process All Pending", command=self._process_all_pending).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ctrl, text="Apply All Ready", command=self._apply_all_ready).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ctrl, text="Clear Done/Skipped", command=self._clear_finished).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(ctrl, text="Auto-process", variable=self.auto_process_var,
                        command=lambda: self.config.set("auto_process", self.auto_process_var.get())).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Checkbutton(ctrl, text="Auto-apply", variable=self.auto_apply_var,
                        command=lambda: self.config.set("auto_apply", self.auto_apply_var.get())).pack(side=tk.LEFT)

        # Treeview
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=6)

        cols = ("file", "suggested", "folder", "status")
        self.queue_tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="browse")
        for col, heading, width in [
            ("file", "Original File", 210),
            ("suggested", "Suggested Name", 210),
            ("folder", "Target Folder", 210),
            ("status", "Status", 110),
        ]:
            self.queue_tree.heading(col, text=heading)
            self.queue_tree.column(col, width=width, minwidth=80)

        self.queue_tree.tag_configure("done", foreground="green")
        self.queue_tree.tag_configure("error", foreground="red")
        self.queue_tree.tag_configure("processing", foreground="gray")

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=vsb.set)
        self.queue_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Item actions
        act = ttk.Frame(parent)
        act.pack(fill=tk.X, padx=6, pady=4)
        ttk.Button(act, text="Apply Selected", command=self._apply_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(act, text="Edit Selected", command=self._edit_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(act, text="Process Selected", command=self._process_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(act, text="Skip Selected", command=self._skip_selected).pack(side=tk.LEFT)

    # ---- Settings tab ------------------------------------------------

    def _build_settings_tab(self, parent):
        f = ttk.Frame(parent, padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        # LMStudio URL
        ttk.Label(f, text="LMStudio API URL:").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(f, textvariable=self.url_var, width=42).grid(row=0, column=1, sticky="ew", padx=(6, 6), pady=6)
        ttk.Button(f, text="Save & Connect", command=self._save_url).grid(row=0, column=2, pady=6)

        # Model selection
        ttk.Label(f, text="AI Model:").grid(row=1, column=0, sticky="w", pady=6)
        self.model_combo = ttk.Combobox(f, textvariable=self.model_var, width=40)
        self.model_combo.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=6)
        ttk.Button(f, text="Refresh", command=self._refresh_models).grid(row=1, column=2, pady=6)

        # Auto-process
        ttk.Label(f, text="Auto-process new files:").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Checkbutton(f, variable=self.auto_process_var,
                        command=lambda: self.config.set("auto_process", self.auto_process_var.get())
                        ).grid(row=2, column=1, sticky="w", padx=(6, 0), pady=6)
        ttk.Label(f, text="Send files to AI automatically when detected.", foreground="gray"
                  ).grid(row=3, column=1, sticky="w", padx=(6, 0))

        # Auto-apply
        ttk.Label(f, text="Auto-apply suggestions:").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Checkbutton(f, variable=self.auto_apply_var,
                        command=lambda: self.config.set("auto_apply", self.auto_apply_var.get())
                        ).grid(row=4, column=1, sticky="w", padx=(6, 0), pady=6)
        ttk.Label(f, text="Rename and move files automatically after AI analysis.", foreground="gray"
                  ).grid(row=5, column=1, sticky="w", padx=(6, 0))

        ttk.Label(f, textvariable=self.settings_status_var, foreground="green"
                  ).grid(row=6, column=0, columnspan=3, sticky="w", pady=10)
        f.columnconfigure(1, weight=1)

    # ---- Folders tab -------------------------------------------------

    def _build_folders_tab(self, parent):
        left = ttk.LabelFrame(parent, text="Configured Folders", padding=6)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 3), pady=6)

        lb_frame = ttk.Frame(left)
        lb_frame.pack(fill=tk.BOTH, expand=True)
        self.folder_listbox = tk.Listbox(lb_frame, selectmode=tk.SINGLE, activestyle="dotbox")
        sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL, command=self.folder_listbox.yview)
        self.folder_listbox.configure(yscrollcommand=sb.set)
        self.folder_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        for folder in self.config.get_folders():
            self.folder_listbox.insert(tk.END, folder)

        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="Edit", command=self._edit_folder).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(btn_row, text="Remove", command=self._remove_folder).pack(side=tk.LEFT)

        right = ttk.LabelFrame(parent, text="Add New Folder", padding=10)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(3, 6), pady=6)

        ttk.Label(right, text="Relative path (e.g. Documents/Work):").pack(anchor="w")
        self.new_folder_entry = ttk.Entry(right, textvariable=self.new_folder_var, width=26)
        self.new_folder_entry.pack(fill=tk.X, pady=(4, 6))
        self.new_folder_entry.bind("<Return>", lambda _: self._add_folder())
        ttk.Button(right, text="Add Folder", command=self._add_folder).pack(fill=tk.X)

        ttk.Separator(right, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)
        ttk.Label(right, text="Output base folder:", font=("", 9, "bold")).pack(anchor="w")
        self.output_folder_label = ttk.Label(
            right,
            text=self.config.get("output_folder") or "(not set)",
            wraplength=190,
            foreground="gray",
        )
        self.output_folder_label.pack(anchor="w", pady=(2, 0))
        ttk.Label(
            right,
            text="Sub-folders are created inside\nthe output folder.",
            foreground="gray",
        ).pack(anchor="w", pady=(6, 0))

    # ---- Log tab -----------------------------------------------------

    def _build_log_tab(self, parent):
        self.log_text = scrolledtext.ScrolledText(
            parent, height=20, state=tk.DISABLED, font=("Consolas", 9), wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Button(btn_row, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    # Watcher control
    # ------------------------------------------------------------------

    def _browse_watch(self):
        folder = filedialog.askdirectory(title="Select Watch Folder")
        if folder:
            self.watch_folder_var.set(folder)
            self.config.set("watch_folder", folder)

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Select Output Folder")
        if folder:
            self.output_folder_var.set(folder)
            self.config.set("output_folder", folder)
            self.output_folder_label.config(text=folder)

    def _start_watching(self):
        watch = self.watch_folder_var.get()
        if not watch:
            messagebox.showwarning("No Folder", "Please select a Watch Folder first.")
            return
        if not os.path.isdir(watch):
            messagebox.showerror("Error", f"Folder does not exist:\n{watch}")
            return
        if not self.config.get("model"):
            messagebox.showwarning("No Model", "Please select an AI model in the Settings tab first.")
            return

        self.watcher = FileWatcher(watch, self._on_new_file)
        self.watcher.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set("Watching")
        self.status_label.config(foreground="green")
        self._log(f"Started watching: {watch}")

    def _stop_watching(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Stopped")
        self.status_label.config(foreground="red")
        self._log("Stopped watching.")

    def _on_new_file(self, filepath):
        """Called from the watcher thread when a new file appears."""
        self._log_thread(f"Detected: {filepath}")
        if self.auto_process_var.get():
            self._proc_queue.put(filepath)
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Queued"))
        else:
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Pending"))

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    def _upsert_queue(self, filepath, suggested, folder, status):
        """Insert or update a row in the queue treeview."""
        filename = os.path.basename(filepath)
        tag = status.lower().split()[0]

        if filepath in self._queue_items:
            iid = self._queue_items[filepath]
            self.queue_tree.item(iid, values=(filename, suggested, folder, status), tags=(tag,))
        else:
            iid = self.queue_tree.insert("", tk.END,
                                         values=(filename, suggested, folder, status),
                                         tags=(tag,))
            self._queue_items[filepath] = iid

    def _filepath_for_item(self, iid):
        for path, item_id in self._queue_items.items():
            if item_id == iid:
                return path
        return None

    def _process_all_pending(self):
        for path, iid in list(self._queue_items.items()):
            values = self.queue_tree.item(iid)["values"]
            if values[3] in ("Pending",):
                self._proc_queue.put(path)
                self._upsert_queue(path, "", "", "Queued")

    def _apply_all_ready(self):
        for path, iid in list(self._queue_items.items()):
            values = self.queue_tree.item(iid)["values"]
            if values[3] == "Ready":
                self._apply_item(path, iid)

    def _clear_finished(self):
        for path, iid in list(self._queue_items.items()):
            values = self.queue_tree.item(iid)["values"]
            if values[3] in ("Done", "Skipped"):
                self.queue_tree.delete(iid)
                del self._queue_items[path]

    def _apply_selected(self):
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Please select a file in the queue.")
            return
        iid = sel[0]
        path = self._filepath_for_item(iid)
        if path:
            self._apply_item(path, iid)

    def _process_selected(self):
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Please select a file in the queue.")
            return
        iid = sel[0]
        path = self._filepath_for_item(iid)
        if path:
            self._proc_queue.put(path)
            self._upsert_queue(path, "", "", "Queued")

    def _skip_selected(self):
        sel = self.queue_tree.selection()
        if not sel:
            return
        iid = sel[0]
        path = self._filepath_for_item(iid)
        if path:
            values = self.queue_tree.item(iid)["values"]
            self._upsert_queue(path, values[1], values[2], "Skipped")

    def _edit_selected(self):
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Please select a file to edit.")
            return
        iid = sel[0]
        path = self._filepath_for_item(iid)
        if not path:
            return
        values = self.queue_tree.item(iid)["values"]
        self._open_edit_dialog(path, iid, values[1], values[2])

    def _open_edit_dialog(self, filepath, iid, current_name, current_folder):
        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Suggestion")
        dlg.geometry("420x200")
        dlg.transient(self.root)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=14)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Suggested filename (no extension):").pack(anchor="w")
        name_var = tk.StringVar(value=current_name)
        ttk.Entry(f, textvariable=name_var, width=48).pack(fill=tk.X, pady=(2, 8))

        ttk.Label(f, text="Target folder:").pack(anchor="w")
        folder_var = tk.StringVar(value=current_folder)
        ttk.Combobox(f, textvariable=folder_var, values=self.config.get_folders(), width=46).pack(fill=tk.X, pady=(2, 10))

        def save():
            new_name = name_var.get().strip()
            new_folder = folder_var.get().strip()
            self._upsert_queue(filepath, new_name, new_folder, "Ready")
            dlg.destroy()

        btn_row = ttk.Frame(f)
        btn_row.pack()
        ttk.Button(btn_row, text="Save", command=save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT, padx=4)

    def _apply_item(self, filepath, iid):
        values = self.queue_tree.item(iid)["values"]
        suggested_name = values[1]
        folder = values[2]

        if not suggested_name:
            messagebox.showwarning("Not Ready", "This file hasn't been analysed yet.")
            return

        output_base = self.output_folder_var.get() or self.watch_folder_var.get()
        try:
            dest_dir = os.path.join(output_base, folder) if folder else output_base
            os.makedirs(dest_dir, exist_ok=True)

            ext = Path(filepath).suffix
            dest_name = f"{suggested_name}{ext}"
            dest_path = os.path.join(dest_dir, dest_name)

            # Avoid overwriting
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(dest_dir, f"{suggested_name}_{counter}{ext}")
                counter += 1

            shutil.move(filepath, dest_path)
            rel = os.path.relpath(dest_path, output_base)
            self._upsert_queue(filepath, suggested_name, folder, "Done")
            self._log(f"Moved: {os.path.basename(filepath)} → {rel}")
        except Exception as exc:
            self._log(f"Error applying {os.path.basename(filepath)}: {exc}")
            messagebox.showerror("Error", f"Could not apply:\n{exc}")

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _save_url(self):
        url = self.url_var.get().strip()
        self.config.set("lmstudio_url", url)
        self.ai_client.base_url = url.rstrip("/")
        self.settings_status_var.set("✓ URL saved")
        self.root.after(2500, lambda: self.settings_status_var.set(""))
        self._check_connection()

    def _refresh_models(self):
        self._log("Fetching models from LMStudio…")

        def fetch():
            try:
                models = self.ai_client.get_models()
                self.root.after(0, lambda: self._update_models(models))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._log(f"Error fetching models: {e}"))

        threading.Thread(target=fetch, daemon=True).start()

    def _update_models(self, models):
        self.model_combo["values"] = models
        if models:
            if not self.model_var.get() or self.model_var.get() not in models:
                self.model_var.set(models[0])
                self.config.set("model", models[0])
            self._log(f"Found {len(models)} model(s): {', '.join(models)}")
        else:
            self._log("No models found. Is LMStudio running with a loaded model?")

    def _check_connection(self):
        def check():
            connected = self.ai_client.is_connected()
            self.root.after(0, lambda c=connected: self._update_conn_ui(c))

        threading.Thread(target=check, daemon=True).start()

    def _update_conn_ui(self, connected):
        if connected:
            self.conn_var.set("Connected ✓")
            self.conn_label.config(foreground="green")
            if not self.model_combo.get():
                self._refresh_models()
        else:
            self.conn_var.set("Not Connected")
            self.conn_label.config(foreground="red")

    def _periodic_save_model(self):
        current = self.model_var.get()
        if current and current != self.config.get("model"):
            self.config.set("model", current)
        self.root.after(2000, self._periodic_save_model)

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    def _add_folder(self):
        folder = self.new_folder_var.get().strip().replace("\\", "/")
        if not folder:
            messagebox.showwarning("Empty", "Please enter a folder path.")
            return
        self.config.add_folder(folder)
        self.folder_listbox.insert(tk.END, folder)
        self.new_folder_var.set("")
        self._log(f"Added folder: {folder}")

    def _edit_folder(self):
        sel = self.folder_listbox.curselection()
        if not sel:
            messagebox.showinfo("No Selection", "Please select a folder to edit.")
            return
        idx = sel[0]
        old = self.folder_listbox.get(idx)

        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Folder")
        dlg.geometry("360x110")
        dlg.transient(self.root)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=12)
        f.pack(fill=tk.BOTH, expand=True)
        ttk.Label(f, text="Folder path:").pack(anchor="w")
        var = tk.StringVar(value=old)
        ttk.Entry(f, textvariable=var, width=42).pack(fill=tk.X, pady=(2, 8))

        def save():
            new = var.get().strip().replace("\\", "/")
            if new:
                folders = self.config.get_folders()
                folders[idx] = new
                self.config.update_folders(folders)
                self.folder_listbox.delete(idx)
                self.folder_listbox.insert(idx, new)
            dlg.destroy()

        btn_row = ttk.Frame(f)
        btn_row.pack()
        ttk.Button(btn_row, text="Save", command=save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT, padx=4)

    def _remove_folder(self):
        sel = self.folder_listbox.curselection()
        if not sel:
            messagebox.showinfo("No Selection", "Please select a folder to remove.")
            return
        folder = self.folder_listbox.get(sel[0])
        if messagebox.askyesno("Remove Folder", f"Remove '{folder}' from the list?"):
            self.config.remove_folder(folder)
            self.folder_listbox.delete(sel[0])

    def _suggest_new_folder(self, folder):
        """Offer to add an AI-suggested folder that isn't yet in the list."""
        if messagebox.askyesno(
            "New Folder Suggested",
            f"The AI suggested a new folder:\n\n  {folder}\n\nAdd it to your folder list?",
        ):
            self.config.add_folder(folder)
            self.folder_listbox.insert(tk.END, folder)

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _log(self, message: str):
        """Append a timestamped message to the log (call from main thread)."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _log_thread(self, message: str):
        """Thread-safe log helper."""
        self.root.after(0, lambda m=message: self._log(m))

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Background processing
    # ------------------------------------------------------------------

    def _process_loop(self):
        """Background thread: pull filepaths from queue and analyse them."""
        import queue as _queue

        while True:
            try:
                filepath = self._proc_queue.get(timeout=1)
            except _queue.Empty:
                continue
            try:
                self._process_file(filepath)
            except Exception as exc:
                self._log_thread(f"Unexpected error processing {os.path.basename(filepath)}: {exc}")
            finally:
                self._proc_queue.task_done()

    def _process_file(self, filepath: str):
        name = os.path.basename(filepath)
        self._log_thread(f"Processing: {name}")
        self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Processing…"))

        if not os.path.exists(filepath):
            self._log_thread(f"File no longer exists: {name}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Error"))
            return

        model = self.config.get("model", "")
        if not model:
            self._log_thread("No AI model selected. Configure one in Settings.")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "No model"))
            return

        try:
            content, file_type = self.file_processor.extract_content(filepath)
            self._log_thread(f"Extracted content ({file_type}) from {name}")
        except Exception as exc:
            self._log_thread(f"Content extraction failed for {name}: {exc}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Error"))
            return

        try:
            folders = self.config.get_folders()
            suggestion = self.ai_client.analyze_file(model, content, file_type, name, folders)
        except Exception as exc:
            self._log_thread(f"AI analysis failed for {name}: {exc}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Error"))
            return

        suggested_name = suggestion.get("filename", "unnamed_file")
        folder = suggestion.get("folder", "Other")
        reason = suggestion.get("reason", "")
        self._log_thread(f"Suggestion: '{suggested_name}' → {folder}  ({reason})")

        # Offer to add unknown folder
        if folder not in self.config.get_folders():
            self.root.after(0, lambda fo=folder: self._suggest_new_folder(fo))

        self.root.after(
            0,
            lambda p=filepath, n=suggested_name, fo=folder: self._upsert_queue(p, n, fo, "Ready"),
        )

        if self.auto_apply_var.get():
            def _auto_apply(p=filepath):
                iid = self._queue_items.get(p)
                if iid:
                    self._apply_item(p, iid)

            self.root.after(200, _auto_apply)

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def on_close(self):
        if self.watcher:
            self.watcher.stop()
        self.root.destroy()
