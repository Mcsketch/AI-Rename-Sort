"""Configuration manager for AI Rename & Sort."""
import json
from pathlib import Path

DEFAULT_CONFIG = {
    "watch_folder": "",
    "output_folder": "",
    "lmstudio_url": "http://localhost:1234",
    "model": "",
    "vision_model": "",
    "text_model": "",
    "folders": [
        # Documents
        "Documents/Finances",
        "Documents/Legal",
        "Documents/Invoices",
        "Documents/Receipts",
        "Documents/Taxes",
        "Documents/Medical",
        "Documents/Housing",
        "Documents/Employment",
        "Documents/Hobbies",
        "Documents/Manuals",
        "Documents/Education",
        "Documents/Identification",
        "Documents/Correspondence",
        "Documents/Subscriptions",
        "Documents/Archives",
        # Pictures
        "Pictures/Family",
        "Pictures/Friends",
        "Pictures/Pets",
        "Pictures/Travel",
        "Pictures/Holidays",
        "Pictures/Home Improvement",
        "Pictures/3D Printing",
        "Pictures/Laser Engraving",
        "Pictures/Screenshots",
        "Pictures/Documents",
        "Pictures/Nature",
        "Pictures/Events",
        "Pictures/Archives",
    ],
    "auto_process": False,
    "auto_apply": False,
    "rescan_interval_secs": 60,
    "rescan_idle_mins": 5,
    "watch_filter": None,
}


class ConfigManager:
    """Manages persistent application configuration stored as JSON."""

    def __init__(self, config_path=None):
        if config_path is None:
            config_dir = Path.home() / ".ai_rename_sort"
            config_dir.mkdir(exist_ok=True)
            config_path = config_dir / "config.json"
        self.config_path = Path(config_path)
        self.config = self._load()

    def _load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(loaded)
                return config
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    def save(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save()

    def get_folders(self):
        return list(self.config.get("folders", []))

    def add_folder(self, folder):
        folders = self.get_folders()
        if folder not in folders:
            folders.append(folder)
            self.set("folders", folders)

    def remove_folder(self, folder):
        folders = self.get_folders()
        if folder in folders:
            folders.remove(folder)
            self.set("folders", folders)

    def update_folders(self, folders):
        self.set("folders", list(folders))

    def get_model_for_type(self, file_type: str) -> str:
        """Return the best model for the given file type.

        Uses the vision model for image/video types and the text model for
        everything else.  Falls back to the other slot or the legacy ``model``
        key if one slot is empty.
        """
        vision_model = self.get("vision_model", "")
        text_model = self.get("text_model", "")
        legacy_model = self.get("model", "")

        if file_type in ("image", "video"):
            return vision_model or text_model or legacy_model
        return text_model or vision_model or legacy_model
