"""Configuration manager for AI Rename & Sort."""
import json
from pathlib import Path

DEFAULT_CONFIG = {
    "watch_folder": "",
    "output_folder": "",
    "lmstudio_url": "http://localhost:1234",
    "model": "",
    "folders": [
        "Documents/Work",
        "Documents/Personal",
        "Documents/Finance",
        "Photos/Events",
        "Photos/Travel",
        "Videos",
        "Downloads/Software",
        "Other",
    ],
    "auto_process": False,
    "auto_apply": False,
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
