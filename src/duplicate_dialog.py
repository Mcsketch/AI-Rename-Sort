"""Side-by-side duplicate resolution dialog."""
import io
import os
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk


# Maximum characters of text content to show in the preview pane
_PREVIEW_CHARS = 400
# Thumbnail size for image previews
_THUMB_SIZE = (200, 200)


class DuplicateDialog(tk.Toplevel):
    """Modal dialog that shows two files side-by-side for duplicate resolution.

    After the dialog closes, inspect the ``result`` attribute which will be
    one of ``"keep_both"``, ``"replace"``, ``"skip"``, or ``None`` if the
    window was closed without choosing.
    """

    def __init__(
        self,
        parent: tk.Tk | tk.Toplevel,
        new_path: str,
        existing_path: str,
        match_type: str = "exact",
        ai_confidence: float | None = None,
        ai_reason: str = "",
    ):
        super().__init__(parent)
        self.title("Possible Duplicate Detected")
        self.geometry("700x520")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self.result: str | None = None
        self._photo_refs: list = []  # prevent GC of PhotoImage objects

        self._build_ui(new_path, existing_path, match_type, ai_confidence, ai_reason)

        # Centre on parent
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, new_path, existing_path, match_type, ai_confidence, ai_reason):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # Header
        match_label = {"exact": "Exact duplicate", "perceptual": "Visually similar",
                       "similar_name": "Similar filename"}.get(match_type, match_type)
        hdr_text = f"Match type: {match_label}"
        if ai_confidence is not None:
            hdr_text += f"   |   AI confidence: {ai_confidence:.0%}"
        if ai_reason:
            hdr_text += f"\n{ai_reason}"

        hdr = ttk.Label(outer, text=hdr_text, wraplength=660, foreground="#555")
        hdr.pack(anchor="w", pady=(0, 8))

        # Two-column comparison
        cols = ttk.Frame(outer)
        cols.pack(fill=tk.BOTH, expand=True)
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)

        self._build_file_column(cols, "New File", new_path, column=0)
        self._build_file_column(cols, "Existing File", existing_path, column=1)

        # Buttons
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill=tk.X, pady=(12, 0))

        ttk.Button(btn_frame, text="Keep Both", command=lambda: self._choose("keep_both")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_frame, text="Replace Existing", command=lambda: self._choose("replace")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_frame, text="Skip New File", command=lambda: self._choose("skip")).pack(
            side=tk.LEFT
        )

    def _build_file_column(self, parent, title: str, filepath: str, column: int):
        frame = ttk.LabelFrame(parent, text=title, padding=6)
        frame.grid(row=0, column=column, sticky="nsew", padx=4)

        # File info
        name = Path(filepath).name
        try:
            size = os.path.getsize(filepath)
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M")
        except OSError:
            size = 0
            mtime = "N/A"

        size_str = self._format_size(size)
        info = f"{name}\nSize: {size_str}\nModified: {mtime}"
        ttk.Label(frame, text=info, wraplength=300, justify="left").pack(anchor="w", pady=(0, 6))

        # Preview
        ext = Path(filepath).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}:
            self._add_image_preview(frame, filepath)
        else:
            self._add_text_preview(frame, filepath)

    def _add_image_preview(self, parent, filepath):
        """Show a thumbnail of the image."""
        try:
            from PIL import Image, ImageTk

            with Image.open(filepath) as img:
                img.thumbnail(_THUMB_SIZE)
                photo = ImageTk.PhotoImage(img)
                self._photo_refs.append(photo)
                lbl = ttk.Label(parent, image=photo)
                lbl.pack(anchor="w")
                return
        except Exception:
            pass
        ttk.Label(parent, text="(preview unavailable)", foreground="gray").pack(anchor="w")

    def _add_text_preview(self, parent, filepath):
        """Show a short text preview of the file."""
        preview = ""
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                preview = f.read(_PREVIEW_CHARS)
        except Exception:
            preview = "(could not read file)"

        text_widget = tk.Text(parent, height=10, width=38, wrap="word", font=("Consolas", 8))
        text_widget.insert("1.0", preview)
        text_widget.config(state="disabled")
        text_widget.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _choose(self, action: str):
        self.result = action
        self.grab_release()
        self.destroy()

    def _on_close(self):
        self.result = None
        self.grab_release()
        self.destroy()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"
