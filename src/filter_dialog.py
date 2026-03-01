"""Pre-flight watch filter dialog – file-type and subfolder selection."""
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

# Extension sets – kept in sync with file_processor.py
_IMG = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
_PDF = {".pdf"}
_VID = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}
_TXT = {
    ".txt", ".md", ".csv", ".log", ".py", ".js", ".ts", ".html", ".xml",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh",
    ".bat", ".css", ".scss", ".rs", ".go", ".java", ".c", ".cpp", ".h",
}
_DOC = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp"}

ALL_KNOWN_EXTS = _IMG | _PDF | _VID | _TXT | _DOC

# (key, display_label, extension_set | None-for-other)
CATEGORIES = [
    ("images",    "Images",         _IMG),
    ("pdfs",      "PDFs",           _PDF),
    ("documents", "Documents",      _DOC),
    ("text",      "Text / Code",    _TXT),
    ("videos",    "Videos",         _VID),
    ("other",     "Other / Unknown", None),
]


class WatchFilterDialog(tk.Toplevel):
    """Modal dialog: scan a folder, let the user choose file types and subfolders.

    After ``wait_window`` returns, check ``self.result``:
    - ``None``  → user cancelled.
    - ``dict``  → runtime filter ready for ``_passes_watch_filter``:
          ``{"allowed_exts": frozenset|None,
             "root_only":    bool,
             "allowed_subdirs": frozenset|None}``
    ``self.result_config`` has the serialisable form for config persistence.
    """

    def __init__(self, parent: tk.Tk, watch_folder: str, saved_filter: dict | None = None):
        super().__init__(parent)
        self.watch_folder = watch_folder
        self.saved_filter: dict = saved_filter or {}
        self.result: dict | None = None
        self.result_config: dict = {}

        self.title("Configure Watch Filter")
        self.geometry("660x580")
        self.minsize(560, 480)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        # Per-category checkbox vars and count labels
        self._cat_vars: dict[str, tk.BooleanVar] = {}
        self._cat_count_lbls: dict[str, ttk.Label] = {}

        # Per-subdir checkbox vars
        self._subdir_vars: dict[str, tk.BooleanVar] = {}

        self._subfolder_mode = tk.StringVar(
            value=self.saved_filter.get("subfolder_mode", "all")
        )
        self._scan_status_var = tk.StringVar(value="Scanning folder…")

        # Canvas/frame refs for subdir list (set in _build_ui)
        self._subdir_inner: ttk.Frame | None = None
        self._subdir_canvas: tk.Canvas | None = None

        self._build_ui()

        # Kick off background folder scan
        threading.Thread(target=self._scan_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ---- Header --------------------------------------------------
        hdr = ttk.Frame(self, padding=(12, 10, 12, 0))
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="Configure Watch Filter",
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(
            hdr,
            text=f"Folder:  {self.watch_folder}",
            foreground="gray",
            wraplength=620,
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            hdr,
            textvariable=self._scan_status_var,
            foreground="#0077CC",
        ).pack(anchor="w", pady=(4, 0))

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=8)

        # ---- Body (two columns) --------------------------------------
        body = ttk.Frame(self, padding=(12, 0, 12, 0))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=5)
        body.columnconfigure(1, weight=4)
        body.rowconfigure(0, weight=1)

        self._build_types_panel(body)
        self._build_subfolders_panel(body)

        # ---- Button bar ----------------------------------------------
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=(8, 0))
        bar = ttk.Frame(self, padding=(12, 6, 12, 10))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Cancel", width=10,
                   command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bar, text="▶  Start Watching", command=self._on_ok).pack(side=tk.RIGHT)

    def _build_types_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="File Types to Process", padding=10)
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        saved_cats = set(
            self.saved_filter.get("allowed_categories", [c[0] for c in CATEGORIES])
        )

        for key, label, _ in CATEGORIES:
            checked = key in saved_cats
            var = tk.BooleanVar(value=checked)
            self._cat_vars[key] = var

            row = ttk.Frame(lf)
            row.pack(fill=tk.X, pady=2)
            ttk.Checkbutton(row, variable=var).pack(side=tk.LEFT)
            ttk.Label(row, text=label, width=16, anchor="w").pack(side=tk.LEFT, padx=(2, 6))
            cnt = ttk.Label(row, text="(scanning…)", foreground="gray", width=14, anchor="w")
            cnt.pack(side=tk.LEFT)
            self._cat_count_lbls[key] = cnt

        # Select All / None
        btn_row = ttk.Frame(lf)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_row, text="Select All", width=11,
                   command=lambda: [v.set(True) for v in self._cat_vars.values()]
                   ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="Select None", width=11,
                   command=lambda: [v.set(False) for v in self._cat_vars.values()]
                   ).pack(side=tk.LEFT)

    def _build_subfolders_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Subfolders", padding=10)
        lf.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        lf.rowconfigure(3, weight=1)
        lf.columnconfigure(0, weight=1)

        ttk.Radiobutton(
            lf, text="Root folder only",
            variable=self._subfolder_mode, value="root_only",
            command=self._refresh_subdir_states,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            lf, text="Include all subfolders",
            variable=self._subfolder_mode, value="all",
            command=self._refresh_subdir_states,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Radiobutton(
            lf, text="Choose specific subfolders:",
            variable=self._subfolder_mode, value="specific",
            command=self._refresh_subdir_states,
        ).grid(row=2, column=0, sticky="w", pady=(4, 6))

        # Scrollable canvas for subdir checkboxes
        outer = ttk.Frame(lf)
        outer.grid(row=3, column=0, sticky="nsew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(win_id, width=e.width),
        )
        # Mouse-wheel scrolling
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        self._subdir_canvas = canvas
        self._subdir_inner = inner

        ttk.Label(inner, text="Scanning…", foreground="gray").pack(anchor="w")

    def _refresh_subdir_states(self):
        """Enable/disable subdir checkboxes based on current radio selection."""
        if self._subdir_inner is None:
            return
        mode = self._subfolder_mode.get()
        state = tk.NORMAL if mode == "specific" else tk.DISABLED
        for w in self._subdir_inner.winfo_children():
            if isinstance(w, (ttk.Checkbutton, tk.Checkbutton)):
                try:
                    w.configure(state=state)
                except tk.TclError:
                    pass
            elif isinstance(w, ttk.Frame):
                for child in w.winfo_children():
                    if isinstance(child, (ttk.Checkbutton, tk.Checkbutton)):
                        try:
                            child.configure(state=state)
                        except tk.TclError:
                            pass

    # ------------------------------------------------------------------
    # Background scan
    # ------------------------------------------------------------------

    def _scan_worker(self):
        watch = Path(self.watch_folder)
        ext_counts: dict[str, int] = {}
        subdir_counts: dict[str, int] = {}
        root_count = 0
        try:
            for root_dir, _dirs, files in os.walk(watch):
                try:
                    rel = Path(root_dir).relative_to(watch)
                except ValueError:
                    continue
                depth = len(rel.parts)
                for fname in files:
                    ext = Path(fname).suffix.lower()
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
                    if depth == 0:
                        root_count += 1
                    else:
                        top = rel.parts[0]
                        subdir_counts[top] = subdir_counts.get(top, 0) + 1
        except Exception:
            pass
        # Hand results back to main thread
        self.after(0, lambda: self._on_scan_done(ext_counts, subdir_counts, root_count))

    def _on_scan_done(self, ext_counts: dict, subdir_counts: dict, root_count: int):
        if not self.winfo_exists():
            return

        total = sum(ext_counts.values())

        # -- Update type counts ----------------------------------------
        for key, _label, exts in CATEGORIES:
            if exts is None:
                count = sum(v for e, v in ext_counts.items() if e not in ALL_KNOWN_EXTS)
            else:
                count = sum(ext_counts.get(e, 0) for e in exts)
            lbl = self._cat_count_lbls.get(key)
            if lbl:
                lbl.config(text=f"({count} file{'s' if count != 1 else ''})")

        self._scan_status_var.set(
            f"Scan complete — {total} file(s) found "
            f"({root_count} in root, {len(subdir_counts)} subfolder(s))."
        )

        # -- Rebuild subdir list ----------------------------------------
        if self._subdir_inner is None:
            return
        for w in self._subdir_inner.winfo_children():
            w.destroy()
        self._subdir_vars.clear()

        if not subdir_counts:
            ttk.Label(
                self._subdir_inner, text="No subfolders found.", foreground="gray"
            ).pack(anchor="w")
        else:
            saved_subs = set(self.saved_filter.get("allowed_subdirs", []))
            mode = self._subfolder_mode.get()
            cb_state = tk.NORMAL if mode == "specific" else tk.DISABLED

            # Show root entry first
            root_row = ttk.Frame(self._subdir_inner)
            root_row.pack(fill=tk.X, pady=1)
            root_var = tk.BooleanVar(value=True)
            self._subdir_vars[""] = root_var
            ttk.Checkbutton(root_row, variable=root_var, state=cb_state).pack(side=tk.LEFT)
            ttk.Label(root_row, text="(root folder)", width=18, anchor="w").pack(side=tk.LEFT, padx=(2, 6))
            ttk.Label(root_row, text=f"({root_count} file{'s' if root_count != 1 else ''})",
                      foreground="gray").pack(side=tk.LEFT)

            for subdir in sorted(subdir_counts):
                count = subdir_counts[subdir]
                default = (subdir in saved_subs) if saved_subs else True
                var = tk.BooleanVar(value=default)
                self._subdir_vars[subdir] = var

                row = ttk.Frame(self._subdir_inner)
                row.pack(fill=tk.X, pady=1)
                ttk.Checkbutton(row, variable=var, state=cb_state).pack(side=tk.LEFT)
                ttk.Label(row, text=subdir, width=18, anchor="w").pack(side=tk.LEFT, padx=(2, 6))
                ttk.Label(row,
                          text=f"({count} file{'s' if count != 1 else ''})",
                          foreground="gray").pack(side=tk.LEFT)

            # Subdir select-all / none buttons
            btn_row = ttk.Frame(self._subdir_inner)
            btn_row.pack(fill=tk.X, pady=(8, 0))
            ttk.Button(btn_row, text="All", width=6,
                       command=lambda: [v.set(True) for v in self._subdir_vars.values()]
                       ).pack(side=tk.LEFT, padx=(0, 3))
            ttk.Button(btn_row, text="None", width=6,
                       command=lambda: [v.set(False) for v in self._subdir_vars.values()]
                       ).pack(side=tk.LEFT)

        if self._subdir_canvas:
            self._subdir_canvas.configure(scrollregion=self._subdir_canvas.bbox("all"))
        self._refresh_subdir_states()

    # ------------------------------------------------------------------
    # OK / build result
    # ------------------------------------------------------------------

    def _on_ok(self):
        selected_cats = [k for k, _l, _e in CATEGORIES if self._cat_vars[k].get()]

        # Build allowed_exts set (None = all)
        if len(selected_cats) == len(CATEGORIES):
            allowed_exts: frozenset | None = None
        else:
            exts_set: set[str] = set()
            for key in selected_cats:
                cat_exts = next(e for k, _l, e in CATEGORIES if k == key)
                if cat_exts is None:
                    # "other" — include any discovered ext not in known sets
                    for e, _cnt in (self._cat_count_lbls or {}).items():
                        pass  # placeholder; filled below via scan data
                    # We don't have ext_counts here directly, so allow any unknown ext
                    # The filter in _passes_watch_filter will handle "other" by
                    # checking that the ext is not in ANY known category set.
                    exts_set.add("__other__")
                else:
                    exts_set |= cat_exts
            allowed_exts = frozenset(exts_set)

        # Build subfolder filter
        mode = self._subfolder_mode.get()
        if mode == "root_only":
            root_only = True
            allowed_subdirs: frozenset | None = None
        elif mode == "all":
            root_only = False
            allowed_subdirs = None
        else:  # specific
            root_only = False
            selected = frozenset(k for k, v in self._subdir_vars.items() if v.get())
            allowed_subdirs = selected

        self.result = {
            "allowed_exts": allowed_exts,
            "root_only": root_only,
            "allowed_subdirs": allowed_subdirs,
            "include_other": "other" in selected_cats,
        }
        self.result_config = {
            "allowed_categories": selected_cats,
            "subfolder_mode": mode,
            "allowed_subdirs": (
                list(allowed_subdirs) if allowed_subdirs is not None else []
            ),
        }
        self.destroy()
