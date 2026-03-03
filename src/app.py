"""Main GUI application for AI Rename & Sort."""
import queue
import re
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .ai_client import LMStudioClient
from .config_manager import ConfigManager
from .duplicate_detector import DuplicateDetector
from .duplicate_dialog import DuplicateDialog
from .file_processor import FileProcessor
from .filter_dialog import ALL_KNOWN_EXTS, WatchFilterDialog
from .watcher import FileWatcher

# Timing constants (milliseconds)
CONNECTION_CHECK_DELAY_MS = 600
MODEL_SAVE_INTERVAL_MS = 2000
STATUS_MESSAGE_DURATION_MS = 2500
AUTO_APPLY_DELAY_MS = 200


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
        self.duplicate_detector = DuplicateDetector()
        self.watcher: FileWatcher | None = None

        # Stores duplicate candidates for files tagged "Duplicate" in the queue
        # filepath -> [(candidate_path, match_type, ai_confidence, ai_reason), ...]
        self._duplicate_info: dict[str, list[tuple]] = {}

        # Stop event – set while watching is paused/stopped, cleared on start
        self._stop_event = threading.Event()
        self._stop_event.set()  # initially stopped

        # Active watch filter (set when Start is clicked; None = no filter)
        self._watch_filter: dict | None = None

        # Processing queue (filepaths to analyse)
        self._proc_queue: queue.Queue[str] = queue.Queue()
        # Maps filepath -> treeview item id
        self._queue_items: dict[str, str] = {}
        # Tracks last rescan time per filepath
        self._rescanned: dict[str, float] = {}

        self._build_ui()

        # Background processing thread
        threading.Thread(target=self._process_loop, daemon=True).start()
        # Background periodic rescan thread
        threading.Thread(target=self._rescan_loop, daemon=True).start()

        # Check LMStudio connection shortly after startup
        self.root.after(CONNECTION_CHECK_DELAY_MS, self._check_connection)
        # Periodic model-save check
        self.root.after(MODEL_SAVE_INTERVAL_MS, self._periodic_save_model)

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
        self.vision_model_var = tk.StringVar(value=self.config.get("vision_model", ""))
        self.text_model_var = tk.StringVar(value=self.config.get("text_model", ""))
        self.new_folder_var = tk.StringVar()
        self.settings_status_var = tk.StringVar()
        self.rescan_interval_var = tk.IntVar(
            value=self.config.get("rescan_interval_secs", 60)
        )
        self.rescan_idle_var = tk.IntVar(
            value=self.config.get("rescan_idle_mins", 5)
        )
        self.max_context_var = tk.IntVar(
            value=self.config.get("max_context_length", 8000)
        )

        # --- New feature vars -----------------------------------------
        self.rename_files_var = tk.BooleanVar(value=self.config.get("rename_files", True))
        self.suggest_similar_title_var = tk.BooleanVar(
            value=self.config.get("suggest_similar_title", False)
        )
        self.naming_style_var = tk.StringVar(value=self.config.get("naming_style", "snake_case"))
        self.prepend_date_var = tk.StringVar(value=self.config.get("prepend_date", "None"))
        self.standardize_ext_var = tk.BooleanVar(
            value=self.config.get("standardize_extensions", True)
        )
        self.folder_mode_var = tk.StringVar(value=self.config.get("folder_mode", "Strict"))
        self.conflict_resolution_var = tk.StringVar(
            value=self.config.get("conflict_resolution", "Auto-increment")
        )

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_top_section(main)

        self._nb = nb = ttk.Notebook(main)
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

        # --- Debug Tab ---
        debug_frame = ttk.Frame(nb)
        nb.add(debug_frame, text="  🔍 Debug  ")
        self._build_debug_tab(debug_frame)

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
        self.queue_tree.tag_configure("rescan", foreground="#0077CC")
        self.queue_tree.tag_configure("duplicate", foreground="#CC7700")

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
        ttk.Button(act, text="Skip Selected", command=self._skip_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(act, text="Resolve Duplicate", command=self._resolve_duplicate_selected).pack(side=tk.LEFT)

    # ---- Settings tab ------------------------------------------------

    def _build_settings_tab(self, parent):
        # Scrollable container
        canvas = tk.Canvas(parent, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(canvas, padding=(12, 8))
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_frame_configure(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e):
            canvas.itemconfig(inner_id, width=e.width)

        def _on_mousewheel(e):
            canvas.yview_scroll(-1 * (e.delta // 120), "units")

        inner.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind("<MouseWheel>", _on_mousewheel)
        inner.bind("<MouseWheel>", _on_mousewheel)

        # ---- Connection & Models ------------------------------------
        lf = ttk.LabelFrame(inner, text="Connection & Models", padding=10)
        lf.pack(fill=tk.X, pady=(0, 8))
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="LMStudio API URL:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(lf, textvariable=self.url_var, width=42).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(lf, text="Save & Connect", command=self._save_url).grid(
            row=0, column=2, pady=4)

        ttk.Label(lf, text="Vision Model:").grid(row=1, column=0, sticky="w", pady=4)
        self.vision_model_combo = ttk.Combobox(lf, textvariable=self.vision_model_var, width=40)
        self.vision_model_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(lf, text="Refresh", command=self._refresh_models).grid(
            row=1, column=2, pady=4)
        ttk.Label(lf, text="Used for images and video frames.", foreground="gray").grid(
            row=2, column=1, sticky="w", padx=6)

        ttk.Label(lf, text="Text Model:").grid(row=3, column=0, sticky="w", pady=4)
        self.text_model_combo = ttk.Combobox(lf, textvariable=self.text_model_var, width=40)
        self.text_model_combo.grid(row=3, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(lf, text="Used for PDFs, documents, text/code, and unknown files.",
                  foreground="gray").grid(row=4, column=1, sticky="w", padx=6)

        # ---- Processing ---------------------------------------------
        lf2 = ttk.LabelFrame(inner, text="Processing", padding=10)
        lf2.pack(fill=tk.X, pady=(0, 8))
        lf2.columnconfigure(1, weight=1)

        ttk.Label(lf2, text="Auto-process new files:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Checkbutton(lf2, variable=self.auto_process_var,
                        command=lambda: self.config.set("auto_process",
                                                        self.auto_process_var.get())
                        ).grid(row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(lf2, text="Send files to AI automatically when detected.",
                  foreground="gray").grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(lf2, text="Auto-apply suggestions:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Checkbutton(lf2, variable=self.auto_apply_var,
                        command=lambda: self.config.set("auto_apply",
                                                        self.auto_apply_var.get())
                        ).grid(row=2, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(lf2, text="Rename and move files automatically after AI analysis.",
                  foreground="gray").grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(lf2, text="Max Context Length (chars):").grid(
            row=4, column=0, sticky="w", pady=4)
        ctx_spin = ttk.Spinbox(lf2, from_=500, to=128000, increment=500,
                               textvariable=self.max_context_var, width=10,
                               command=self._save_context_length)
        ctx_spin.grid(row=4, column=1, sticky="w", padx=6, pady=4)
        ctx_spin.bind("<FocusOut>", self._save_context_length)
        ctx_spin.bind("<Return>", self._save_context_length)
        ttk.Label(lf2,
                  text="Characters of file content sent to the AI. Lower = faster; higher = more context.",
                  foreground="gray").grid(row=5, column=1, sticky="w", padx=6)

        # ---- Renaming Options ---------------------------------------
        lf3 = ttk.LabelFrame(inner, text="Renaming Options", padding=10)
        lf3.pack(fill=tk.X, pady=(0, 8))
        lf3.columnconfigure(1, weight=1)

        ttk.Label(lf3, text="Rename files:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Checkbutton(lf3, variable=self.rename_files_var,
                        command=lambda: self.config.set("rename_files",
                                                        self.rename_files_var.get())
                        ).grid(row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(lf3, text="When off, only folder sorting is performed.",
                  foreground="gray").grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(lf3, text="Suggest similar title:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Checkbutton(lf3, variable=self.suggest_similar_title_var,
                        command=lambda: self.config.set("suggest_similar_title",
                                                        self.suggest_similar_title_var.get())
                        ).grid(row=2, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(lf3,
                  text="Keep the AI filename close to the original title (clean-up only).",
                  foreground="gray").grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(lf3, text="Naming style:").grid(row=4, column=0, sticky="w", pady=4)
        naming_combo = ttk.Combobox(lf3, textvariable=self.naming_style_var, state="readonly",
                                    width=22,
                                    values=["snake_case", "kebab-case", "CamelCase", "Spaces Allowed"])
        naming_combo.grid(row=4, column=1, sticky="w", padx=6, pady=4)
        self.naming_style_var.trace_add("write",
            lambda *_: self.config.set("naming_style", self.naming_style_var.get()))
        ttk.Label(lf3, text="Format applied to AI-generated filenames.",
                  foreground="gray").grid(row=5, column=1, sticky="w", padx=6)

        ttk.Label(lf3, text="Prepend date:").grid(row=6, column=0, sticky="w", pady=4)
        date_combo = ttk.Combobox(lf3, textvariable=self.prepend_date_var, state="readonly",
                                  width=22, values=["None", "File Creation Date"])
        date_combo.grid(row=6, column=1, sticky="w", padx=6, pady=4)
        self.prepend_date_var.trace_add("write",
            lambda *_: self.config.set("prepend_date", self.prepend_date_var.get()))
        ttk.Label(lf3, text="Optionally prefix YYYY-MM-DD_ to the final filename.",
                  foreground="gray").grid(row=7, column=1, sticky="w", padx=6)

        ttk.Label(lf3, text="Standardize extensions:").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Checkbutton(lf3, variable=self.standardize_ext_var,
                        command=lambda: self.config.set("standardize_extensions",
                                                        self.standardize_ext_var.get())
                        ).grid(row=8, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(lf3,
                  text="Map .jpeg\u2192.jpg, .htm\u2192.html, .tif\u2192.tiff, etc. and lowercase ext.",
                  foreground="gray").grid(row=9, column=1, sticky="w", padx=6)

        # ---- Folder Options -----------------------------------------
        lf4 = ttk.LabelFrame(inner, text="Folder Options", padding=10)
        lf4.pack(fill=tk.X, pady=(0, 8))
        lf4.columnconfigure(1, weight=1)

        ttk.Label(lf4, text="Folder mode:").grid(row=0, column=0, sticky="w", pady=4)
        folder_combo = ttk.Combobox(lf4, textvariable=self.folder_mode_var, state="readonly",
                                    width=22, values=["Strict", "Flexible"])
        folder_combo.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        self.folder_mode_var.trace_add("write",
            lambda *_: self.config.set("folder_mode", self.folder_mode_var.get()))
        ttk.Label(lf4,
                  text="Strict: AI picks from your list only.  Flexible: AI may suggest new subfolders.",
                  foreground="gray").grid(row=1, column=1, sticky="w", padx=6)

        # ---- File Operations ----------------------------------------
        lf5 = ttk.LabelFrame(inner, text="File Operations", padding=10)
        lf5.pack(fill=tk.X, pady=(0, 8))
        lf5.columnconfigure(1, weight=1)

        ttk.Label(lf5, text="Conflict resolution:").grid(row=0, column=0, sticky="w", pady=4)
        conflict_combo = ttk.Combobox(lf5, textvariable=self.conflict_resolution_var,
                                      state="readonly", width=22,
                                      values=["Auto-increment", "Overwrite", "Skip"])
        conflict_combo.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        self.conflict_resolution_var.trace_add("write",
            lambda *_: self.config.set("conflict_resolution",
                                       self.conflict_resolution_var.get()))
        ttk.Label(lf5,
                  text="What to do when a file already exists at the destination.",
                  foreground="gray").grid(row=1, column=1, sticky="w", padx=6)

        # ---- Periodic Rescan ----------------------------------------
        lf6 = ttk.LabelFrame(inner, text="Periodic Rescan", padding=10)
        lf6.pack(fill=tk.X, pady=(0, 8))
        lf6.columnconfigure(1, weight=1)

        ttk.Label(lf6, text="Interval between files (sec):").grid(
            row=0, column=0, sticky="w", pady=4)
        interval_spin = ttk.Spinbox(lf6, from_=10, to=3600, increment=10,
                                    textvariable=self.rescan_interval_var, width=8,
                                    command=self._save_rescan_settings)
        interval_spin.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        interval_spin.bind("<FocusOut>", self._save_rescan_settings)
        interval_spin.bind("<Return>", self._save_rescan_settings)
        ttk.Label(lf6,
                  text="Seconds to wait between enqueueing each file during a rescan pass.",
                  foreground="gray").grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(lf6, text="Idle between passes (min):").grid(
            row=2, column=0, sticky="w", pady=4)
        idle_spin = ttk.Spinbox(lf6, from_=1, to=720, increment=1,
                                textvariable=self.rescan_idle_var, width=8,
                                command=self._save_rescan_settings)
        idle_spin.grid(row=2, column=1, sticky="w", padx=6, pady=4)
        idle_spin.bind("<FocusOut>", self._save_rescan_settings)
        idle_spin.bind("<Return>", self._save_rescan_settings)
        ttk.Label(lf6,
                  text="Minutes to wait after completing a full rescan pass before starting the next.",
                  foreground="gray").grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(inner, textvariable=self.settings_status_var, foreground="green"
                  ).pack(anchor="w", pady=(4, 0))

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

    # ---- Debug tab ---------------------------------------------------

    def _build_debug_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(top, text="AI Payload Inspector", font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Clear", command=self._clear_debug).pack(side=tk.RIGHT)
        ttk.Label(
            top,
            text="  Shows the exact content sent to and received from the AI for each file.",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=(8, 0))

        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        # Left: payload (what was sent)
        left_lf = ttk.LabelFrame(paned, text="Sent to AI", padding=4)
        paned.add(left_lf, weight=3)
        self._debug_sent = scrolledtext.ScrolledText(
            left_lf, font=("Consolas", 8), wrap=tk.WORD, state=tk.DISABLED,
            bg="#1e1e1e", fg="#9cdcfe", insertbackground="white",
        )
        self._debug_sent.pack(fill=tk.BOTH, expand=True)

        # Right: raw response + parsed result
        right_lf = ttk.LabelFrame(paned, text="AI Response", padding=4)
        paned.add(right_lf, weight=2)
        self._debug_resp = scrolledtext.ScrolledText(
            right_lf, font=("Consolas", 8), wrap=tk.WORD, state=tk.DISABLED,
            bg="#1e1e1e", fg="#ce9178", insertbackground="white",
        )
        self._debug_resp.pack(fill=tk.BOTH, expand=True)

    def _clear_debug(self):
        for widget in (self._debug_sent, self._debug_resp):
            widget.config(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.config(state=tk.DISABLED)

    def _log_debug_payload(
        self,
        filepath: str,
        content: str,
        file_type: str,
        messages: list,
        raw_response: str,
        suggestion: dict,
    ):
        """Write AI payload and response to the Debug tab (main thread only)."""
        sep = "=" * 60 + "\n"

        # ---- Sent panel ----------------------------------------------
        # Reconstruct the text portions of the message list for display
        sent_lines = [sep, f"FILE:      {filepath}\n", f"TYPE:      {file_type}\n",
                      f"CONTENT:   {len(content) if not content.startswith('data:') else '(binary/base64)'} chars\n"]

        if not content.startswith("data:"):
            preview_len = min(len(content), 2000)
            sent_lines.append(f"\nCONTENT PREVIEW ({preview_len} of {len(content)} chars):\n")
            sent_lines.append(content[:preview_len])
            if len(content) > preview_len:
                sent_lines.append(f"\n… [{len(content) - preview_len} chars truncated]")
        else:
            sent_lines.append("\nCONTENT: <base64 image/video — not shown>")

        sent_lines.append("\n\nPROMPT MESSAGES:\n")
        for msg in messages:
            role = msg.get("role", "?").upper()
            body = msg.get("content", "")
            if isinstance(body, list):
                # Multimodal — show text parts only
                text_parts = [p.get("text", "") for p in body if p.get("type") == "text"]
                body = "\n".join(text_parts) + "\n<image_url omitted>"
            sent_lines.append(f"[{role}]\n{body}\n\n")

        sent_text = "".join(sent_lines)

        # ---- Response panel ------------------------------------------
        resp_lines = [
            sep,
            f"FILE:  {Path(filepath).name}\n\n",
            "RAW RESPONSE:\n",
            raw_response or "(empty)",
            "\n\nPARSED RESULT:\n",
            f"  filename : {suggestion.get('filename', '?')}\n",
            f"  folder   : {suggestion.get('folder', '?')}\n",
            f"  reason   : {suggestion.get('reason', '?')}\n",
        ]
        resp_text = "".join(resp_lines)

        def _write():
            for widget, text in (
                (self._debug_sent, sent_text),
                (self._debug_resp, resp_text),
            ):
                widget.config(state=tk.NORMAL)
                widget.insert(tk.END, text)
                widget.see(tk.END)
                widget.config(state=tk.DISABLED)

        self.root.after(0, _write)

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
        if not Path(watch).is_dir():
            messagebox.showerror("Error", f"Folder does not exist:\n{watch}")
            return
        if not self.config.get("vision_model") and not self.config.get("text_model"):
            messagebox.showwarning("No Model", "Please select at least one AI model in the Settings tab first.")
            return

        # Show the file-type / subfolder filter dialog
        dlg = WatchFilterDialog(self.root, watch, self.config.get("watch_filter"))
        self.root.wait_window(dlg)
        if dlg.result is None:
            return  # user cancelled

        self._watch_filter = dlg.result
        self.config.set("watch_filter", dlg.result_config)

        self._stop_event.clear()  # allow _process_loop and _rescan_loop to run

        self.watcher = FileWatcher(watch, self._on_new_file)
        self.watcher.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set("Watching")
        self.status_label.config(foreground="green")
        self._log(f"Started watching: {watch}")
        self._log(self._filter_summary())

    def _stop_watching(self):
        self._stop_event.set()  # halt _process_loop after current file finishes
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
        if not self._passes_watch_filter(filepath):
            return
        self._log_thread(f"Detected: {filepath}")
        if self.auto_process_var.get():
            self._proc_queue.put(filepath)
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Queued"))
        else:
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Pending"))

    def _passes_watch_filter(self, filepath: str) -> bool:
        """Return True if *filepath* matches the active watch filter."""
        wf = self._watch_filter
        if not wf:
            return True

        fpath = Path(filepath)
        ext = fpath.suffix.lower()

        # ---- Extension / type filter ---------------------------------
        allowed_exts: frozenset | None = wf.get("allowed_exts")
        include_other: bool = wf.get("include_other", True)

        if allowed_exts is not None:
            # "__other__" sentinel means we accept unknown extensions too
            if "__other__" in allowed_exts:
                passes_ext = (ext in allowed_exts) or (ext not in ALL_KNOWN_EXTS)
            else:
                passes_ext = ext in allowed_exts
            if not passes_ext:
                return False

        # ---- Subfolder filter ----------------------------------------
        watch = self.config.get("watch_folder", "")
        root_only: bool = wf.get("root_only", False)
        allowed_subdirs: frozenset | None = wf.get("allowed_subdirs")

        if root_only or allowed_subdirs is not None:
            try:
                rel = fpath.relative_to(watch)
            except ValueError:
                return True  # can't determine — allow

            depth = len(rel.parts)

            if root_only:
                return depth == 1  # must be directly in watch folder

            # specific-subfolder mode
            if depth == 1:
                # File is in root — include if root ("") is in the set
                return "" in allowed_subdirs  # type: ignore[operator]
            # File is in a subfolder
            return rel.parts[0] in allowed_subdirs  # type: ignore[operator]

        return True

    def _filter_summary(self) -> str:
        """Return a human-readable description of the active watch filter."""
        wf = self._watch_filter
        if not wf:
            return "Filter: all file types, all subfolders."

        cfg = self.config.get("watch_filter") or {}
        cats = cfg.get("allowed_categories", [])
        mode = cfg.get("subfolder_mode", "all")
        subdirs = cfg.get("allowed_subdirs", [])

        type_part = ", ".join(cats) if cats else "no types"
        if mode == "root_only":
            dir_part = "root folder only"
        elif mode == "all":
            dir_part = "all subfolders"
        else:
            listed = ", ".join(subdirs) if subdirs else "(none)"
            dir_part = f"subfolders: {listed}"

        return f"Filter active — types: {type_part}  |  {dir_part}."

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    def _upsert_queue(self, filepath, suggested, folder, status):
        """Insert or update a row in the queue treeview."""
        filename = Path(filepath).name
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

    # Extension standardisation map
    _EXT_MAP = {
        ".jpeg": ".jpg",
        ".jfif": ".jpg",
        ".htm":  ".html",
        ".tif":  ".tiff",
        ".mpeg": ".mpg",
    }

    def _apply_item(self, filepath, iid):
        values = self.queue_tree.item(iid)["values"]
        suggested_name = values[1]
        folder = values[2]

        if not suggested_name:
            messagebox.showwarning("Not Ready", "This file hasn't been analysed yet.")
            return

        output_base = self.output_folder_var.get() or self.watch_folder_var.get()
        try:
            dest_dir = Path(output_base) / folder if folder else Path(output_base)
            dest_dir.mkdir(parents=True, exist_ok=True)

            # --- Extension standardization ----------------------------
            ext = Path(filepath).suffix.lower()
            if self.config.get("standardize_extensions", True):
                ext = self._EXT_MAP.get(ext, ext)

            # --- Date prefixing ---------------------------------------
            final_name = suggested_name
            if self.config.get("prepend_date", "None") == "File Creation Date":
                try:
                    ctime = Path(filepath).stat().st_ctime
                    date_prefix = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d_")
                    if not final_name.startswith(date_prefix):
                        final_name = date_prefix + final_name
                except OSError:
                    pass  # can't read ctime — skip prefix

            dest_name = f"{final_name}{ext}"
            dest_path = dest_dir / dest_name

            # --- Conflict resolution ----------------------------------
            strategy = self.config.get("conflict_resolution", "Auto-increment")

            if dest_path.exists():
                if strategy == "Skip":
                    self._log(f"Skipped (exists): {dest_name}")
                    self._upsert_queue(filepath, suggested_name, folder, "Skipped")
                    return
                elif strategy == "Overwrite":
                    pass  # shutil.move will overwrite
                else:  # Auto-increment (default)
                    counter = 1
                    while dest_path.exists():
                        dest_path = dest_dir / f"{final_name}_{counter}{ext}"
                        counter += 1

            shutil.move(filepath, dest_path)
            rel = dest_path.relative_to(output_base)
            self._upsert_queue(filepath, suggested_name, folder, "Done")
            self._log(f"Moved: {Path(filepath).name} → {rel}")
        except Exception as exc:
            self._log(f"Error applying {Path(filepath).name}: {exc}")
            messagebox.showerror("Error", f"Could not apply:\n{exc}")

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _save_url(self):
        url = self.url_var.get().strip()
        self.config.set("lmstudio_url", url)
        self.ai_client.base_url = url.rstrip("/")
        self.settings_status_var.set("✓ URL saved")
        self.root.after(STATUS_MESSAGE_DURATION_MS, lambda: self.settings_status_var.set(""))
        self._check_connection()

    def _refresh_models(self):
        self._log("Fetching models from LMStudio…")

        def fetch():
            try:
                models = self.ai_client.get_models()
                assignments = LMStudioClient.auto_assign_models(models)
                self.root.after(0, lambda: self._update_models(models, assignments))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._log(f"Error fetching models: {e}"))

        threading.Thread(target=fetch, daemon=True).start()

    def _update_models(self, models, assignments: dict[str, str] | None = None):
        self.vision_model_combo["values"] = models
        self.text_model_combo["values"] = models

        if models:
            # Auto-assign if combos are empty or current selection is stale
            if assignments:
                if not self.vision_model_var.get() or self.vision_model_var.get() not in models:
                    suggested = assignments.get("vision_model", "")
                    self.vision_model_var.set(suggested or models[0])
                if not self.text_model_var.get() or self.text_model_var.get() not in models:
                    suggested = assignments.get("text_model", "")
                    self.text_model_var.set(suggested or models[0])

            # Persist
            self.config.set("vision_model", self.vision_model_var.get())
            self.config.set("text_model", self.text_model_var.get())

            vision_type = LMStudioClient.classify_model(self.vision_model_var.get()) if self.vision_model_var.get() else "?"
            text_type = LMStudioClient.classify_model(self.text_model_var.get()) if self.text_model_var.get() else "?"
            self._log(
                f"Found {len(models)} model(s). "
                f"Vision → {self.vision_model_var.get() or '(none)'} [{vision_type}], "
                f"Text → {self.text_model_var.get() or '(none)'} [{text_type}]"
            )
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
            if not self.vision_model_combo.get() and not self.text_model_combo.get():
                self._refresh_models()
        else:
            self.conn_var.set("Not Connected")
            self.conn_label.config(foreground="red")

    def _periodic_save_model(self):
        v = self.vision_model_var.get()
        t = self.text_model_var.get()
        if v and v != self.config.get("vision_model"):
            self.config.set("vision_model", v)
        if t and t != self.config.get("text_model"):
            self.config.set("text_model", t)
        self.root.after(MODEL_SAVE_INTERVAL_MS, self._periodic_save_model)

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
            # If stopped, put the item back and wait until resumed
            if self._stop_event.is_set():
                self._proc_queue.put(filepath)
                self._proc_queue.task_done()
                while self._stop_event.is_set():
                    time.sleep(0.5)
                continue
            try:
                self._process_file(filepath)
            except Exception as exc:
                self._log_thread(f"Unexpected error processing {Path(filepath).name}: {exc}")
            finally:
                self._proc_queue.task_done()

    def _process_file(self, filepath: str):
        name = Path(filepath).name
        self._log_thread(f"Processing: {name}")
        self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Processing…"))

        if not Path(filepath).exists():
            self._log_thread(f"File no longer exists: {name}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Error"))
            return

        max_length = self.config.get("max_context_length", 8000)

        # --- Extract content & determine file type --------------------
        try:
            content, file_type = self.file_processor.extract_content(filepath, max_length=max_length)
            self._log_thread(f"Extracted content ({file_type}) from {name}")
        except Exception as exc:
            self._log_thread(f"Content extraction failed for {name}: {exc}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Error"))
            return

        # Bail early if Stop was clicked during content extraction
        if self._stop_event.is_set():
            self._log_thread(f"Stopped before processing: {name}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Pending"))
            return

        # --- Select the right model for this file type ----------------
        model = self.config.get_model_for_type(file_type)
        if not model:
            self._log_thread("No AI model selected. Configure one in Settings.")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "No model"))
            return

        self._log_thread(f"Using model '{model}' for {file_type} file")

        # --- Duplicate detection --------------------------------------
        search_dirs = []
        watch = self.config.get("watch_folder", "")
        output = self.config.get("output_folder", "")
        if watch:
            search_dirs.append(watch)
        if output and output != watch:
            search_dirs.append(output)

        if search_dirs:
            try:
                candidates = self.duplicate_detector.find_candidates(filepath, search_dirs)
            except Exception as exc:
                self._log_thread(f"Duplicate check error: {exc}")
                candidates = []

            if candidates:
                dup_result = self._handle_duplicates(filepath, content, file_type, name, model, candidates)
                if dup_result == "skip":
                    self._log_thread(f"Skipped (duplicate): {name}")
                    self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Skipped"))
                    return
                elif dup_result == "replace":
                    pass
                # "keep_both" or None → continue normally

        # Bail again if Stop was clicked during duplicate detection
        if self._stop_event.is_set():
            self._log_thread(f"Stopped before AI call: {name}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Pending"))
            return

        # --- AI analysis ----------------------------------------------
        rename_files = self.config.get("rename_files", True)
        suggest_similar_title = self.config.get("suggest_similar_title", False)
        folder_mode = self.config.get("folder_mode", "Strict")
        naming_style = self.config.get("naming_style", "snake_case")

        try:
            folders = self.config.get_folders()
            suggestion = self.ai_client.analyze_file(
                model, content, file_type, name, folders,
                max_length=max_length,
                rename_files=rename_files,
                suggest_similar_title=suggest_similar_title,
                folder_mode=folder_mode,
                naming_style=naming_style,
            )
        except Exception as exc:
            self._log_thread(f"AI analysis failed for {name}: {exc}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Error"))
            return

        # Bail if Stop was clicked while the AI call was in-flight
        if self._stop_event.is_set():
            self._log_thread(f"Stopped after AI returned (result discarded): {name}")
            self.root.after(0, lambda p=filepath: self._upsert_queue(p, "", "", "Pending"))
            return

        # When rename_files=False the AI returns an empty filename; use the stem
        suggested_name = suggestion.get("filename") or Path(filepath).stem
        folder = suggestion.get("folder", "Other")
        reason = suggestion.get("reason", "")
        self._log_thread(f"Suggestion: '{suggested_name}' → {folder}  ({reason})")

        # Log to debug tab — capture before next file overwrites them
        _dbg_messages = list(self.ai_client._last_messages)
        _dbg_raw = self.ai_client._last_raw_response
        _dbg_content = content if not isinstance(content, bytes) else "<binary>"
        self._log_debug_payload(filepath, _dbg_content, file_type, _dbg_messages, _dbg_raw, suggestion)

        # Offer to add unknown folder (only when actively watching)
        if folder not in self.config.get_folders() and not self._stop_event.is_set():
            self.root.after(0, lambda fo=folder: self._suggest_new_folder(fo))

        self.root.after(
            0,
            lambda p=filepath, n=suggested_name, fo=folder: self._upsert_queue(p, n, fo, "Ready"),
        )

        if self.auto_apply_var.get() and not self._stop_event.is_set():
            def _auto_apply(p=filepath):
                if not self._stop_event.is_set():
                    iid = self._queue_items.get(p)
                    if iid:
                        self._apply_item(p, iid)

            self.root.after(AUTO_APPLY_DELAY_MS, _auto_apply)

    # ------------------------------------------------------------------
    # Duplicate handling
    # ------------------------------------------------------------------

    def _handle_duplicates(
        self,
        filepath: str,
        content,
        file_type: str,
        filename: str,
        model: str,
        candidates: list[tuple[str, str]],
    ) -> str | None:
        """Tag duplicate files and skip them for later manual review.

        Instead of blocking the processing thread with a modal dialog, this
        method records each confirmed duplicate candidate, marks the file as
        "Duplicate" in the queue (orange), and returns ``"skip"`` so that
        processing continues with the next file.  The user can later select
        the item and click **Resolve Duplicate** to choose what to do.

        Returns ``"skip"`` when at least one confirmed candidate is found,
        ``None`` otherwise.
        """
        confirmed: list[tuple] = []

        for candidate_path, match_type in candidates:
            ai_confidence: float | None = None
            ai_reason = ""

            if match_type == "exact":
                # No AI needed – it's an identical file
                ai_confidence = 1.0
                ai_reason = "Files are byte-for-byte identical."
            else:
                # Ask AI to confirm near-duplicate
                try:
                    cand_content, cand_type = self.file_processor.extract_content(candidate_path)
                    cand_name = Path(candidate_path).name
                    result = self.ai_client.compare_for_duplicate(
                        model, content, file_type, filename,
                        cand_content, cand_type, cand_name,
                    )
                    if not result.get("is_duplicate", False) or result.get("confidence", 0) < 0.7:
                        continue  # AI says it's not a real duplicate
                    ai_confidence = result.get("confidence", 0.0)
                    ai_reason = result.get("reason", "")
                except Exception as exc:
                    self._log_thread(f"AI duplicate comparison failed: {exc}")
                    continue

            self._log_thread(
                f"Duplicate candidate: {Path(candidate_path).name} "
                f"({match_type}, confidence={ai_confidence})"
            )
            confirmed.append((candidate_path, match_type, ai_confidence, ai_reason))

        if confirmed:
            # Store info for later resolution and tag the item in the queue
            self._duplicate_info[filepath] = confirmed
            self.root.after(
                0,
                lambda p=filepath: self._upsert_queue(p, "", "", "Duplicate"),
            )
            return "skip"

        return None  # no confirmed duplicates

    def _resolve_duplicate_selected(self):
        """Open the DuplicateDialog for the selected 'Duplicate' queue item.

        The user can choose Keep Both, Replace Existing, or Skip New File.
        The decision is applied immediately and the item is re-queued for AI
        processing (Keep Both / Replace) or permanently skipped (Skip).
        """
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Please select a Duplicate item in the queue.")
            return
        iid = sel[0]
        filepath = self._filepath_for_item(iid)
        if not filepath:
            return

        values = self.queue_tree.item(iid)["values"]
        if str(values[3]).lower() != "duplicate":
            messagebox.showinfo(
                "Not a Duplicate",
                "The selected item is not tagged as a duplicate.\n"
                "Only items with status 'Duplicate' can be resolved here.",
            )
            return

        candidates = self._duplicate_info.get(filepath, [])
        if not candidates:
            # No stored info – just re-queue for normal processing
            self._duplicate_info.pop(filepath, None)
            self._proc_queue.put(filepath)
            self._upsert_queue(filepath, "", "", "Queued")
            return

        # Show the dialog for the first confirmed candidate
        candidate_path, match_type, ai_confidence, ai_reason = candidates[0]

        dlg = DuplicateDialog(
            self.root, filepath, candidate_path,
            match_type=match_type,
            ai_confidence=ai_confidence,
            ai_reason=ai_reason,
        )
        self.root.wait_window(dlg)
        action = dlg.result

        # Clean up stored duplicate info regardless of action
        self._duplicate_info.pop(filepath, None)

        if action == "replace":
            try:
                Path(candidate_path).unlink()
                self._log(f"Deleted existing duplicate: {candidate_path}")
            except OSError as exc:
                self._log(f"Could not delete existing file: {exc}")
            # Re-queue for AI analysis now that the conflict is gone
            self._proc_queue.put(filepath)
            self._upsert_queue(filepath, "", "", "Queued")

        elif action == "keep_both":
            # Re-queue; the filename conflict will be resolved by auto-increment
            self._proc_queue.put(filepath)
            self._upsert_queue(filepath, "", "", "Queued")

        else:
            # "skip" or dialog dismissed – mark permanently skipped
            self._upsert_queue(filepath, "", "", "Skipped")

    # ------------------------------------------------------------------
    # Periodic rescan
    # ------------------------------------------------------------------

    def _save_rescan_settings(self, *_):
        """Persist rescan interval/idle settings from the Settings spin boxes."""
        try:
            interval = int(self.rescan_interval_var.get())
            idle = int(self.rescan_idle_var.get())
        except (ValueError, tk.TclError):
            return
        self.config.set("rescan_interval_secs", max(10, interval))
        self.config.set("rescan_idle_mins", max(1, idle))

    def _save_context_length(self, *_):
        """Persist max context length from the Settings spinbox."""
        try:
            value = int(self.max_context_var.get())
        except (ValueError, tk.TclError):
            return
        self.config.set("max_context_length", max(500, value))

    def _on_rescan_file(self, filepath: str):
        """Schedule a file for re-evaluation during a periodic rescan pass."""
        if not Path(filepath).exists():
            return
        self._log(f"Rescan queued: {Path(filepath).name}")
        self._proc_queue.put(filepath)
        self._upsert_queue(filepath, "", "", "Rescan")

    def _rescan_loop(self):
        """Background thread: periodically re-evaluate all files for AI accuracy."""
        # Let the app fully start up before first scan
        time.sleep(10)

        while True:
            # Wait if watching is stopped
            if self._stop_event.is_set():
                time.sleep(10)
                continue

            watch = self.config.get("watch_folder", "")
            output = self.config.get("output_folder", "")

            if not watch:
                time.sleep(30)
                continue

            interval_secs = max(10, self.config.get("rescan_interval_secs", 60))
            idle_mins = max(1, self.config.get("rescan_idle_mins", 5))
            now = time.time()

            # Build list of directories to scan
            scan_dirs: list[Path] = []
            if Path(watch).is_dir():
                scan_dirs.append(Path(watch))
            if output and output != watch and Path(output).is_dir():
                scan_dirs.append(Path(output))

            files_found = 0
            for scan_dir in scan_dirs:
                for fpath_obj in scan_dir.rglob("*"):
                    if self._stop_event.is_set():
                        break
                    if not fpath_obj.is_file():
                        continue
                    fpath = str(fpath_obj)
                    # Respect the watch filter during periodic rescans
                    if not self._passes_watch_filter(fpath):
                        continue
                    last_scan = self._rescanned.get(fpath, 0.0)
                    if now - last_scan >= interval_secs:
                        self._rescanned[fpath] = now
                        self.root.after(0, lambda p=fpath: self._on_rescan_file(p))
                        files_found += 1
                        # Throttle: wait between each file to stay slow/accurate
                        time.sleep(interval_secs)
                        now = time.time()
                if self._stop_event.is_set():
                    break

            if not self._stop_event.is_set():
                self._log_thread(
                    f"Rescan pass complete — {files_found} file(s) queued. "
                    f"Next pass in {idle_mins} min."
                )
                time.sleep(idle_mins * 60)

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def on_close(self):
        self._stop_event.set()
        if self.watcher:
            self.watcher.stop()
        self.root.destroy()
