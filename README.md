# AI Rename & Sort

A desktop GUI application that watches a folder for new files, uses a local
[LMStudio](https://lmstudio.ai/) AI model to understand each file's content,
then suggests a descriptive filename and target folder – and can apply the
changes automatically.

## Features

- **Folder watcher** – monitors a chosen directory; detects files as soon as they
  finish being written.
- **AI analysis** – sends file content to any model loaded in LMStudio:
  - *Images* → base64-encoded and sent to a vision-capable model.
  - *PDFs* → text extracted (up to 10 pages) and sent as a prompt.
  - *Videos* → first representative frame extracted (requires OpenCV) or
    metadata sent as a fallback.
  - *Plain-text / code / config files* → content sent directly.
  - *Office documents* (`.docx`, `.xlsx`) → text extracted when the optional
    `python-docx` / `openpyxl` libraries are present.
- **Smart naming** – AI returns a clean, descriptive filename (no extension,
  underscores instead of spaces).
- **Folder suggestions** – AI picks the best match from your configured folder
  list; unknown folders are offered for addition.
- **Queue UI** – review every suggestion before applying, or enable
  *Auto-process* + *Auto-apply* for fully hands-off operation.
- **Persistent config** – settings and folder list saved to
  `~/.ai_rename_sort/config.json`.

## Requirements

- Python 3.10+
- [LMStudio](https://lmstudio.ai/) running locally with at least one model
  loaded (default API endpoint: `http://localhost:1234`).

## Installation

```bash
pip install -r requirements.txt
```

Optional extras for better file-type support:

```bash
pip install python-docx openpyxl opencv-python
```

## Usage

```bash
python main.py
```

1. **Watch Folder** – click *Browse…* and pick the folder to monitor.
2. **Output Folder** – click *Browse…* and pick where sorted files will be
   placed (can be the same folder if you prefer in-place renaming).
3. **Settings tab** – enter your LMStudio URL, click *Refresh* to load
   available models, then select one.
4. **Folders tab** – add or edit the folder paths that the AI will choose from.
5. Click **▶ Start Watching**.

New files will appear in the **Queue** tab.  Click *Apply Selected* to move
and rename a file, or enable *Auto-apply* to have it done automatically.

## Building a standalone executable

```bash
pip install pyinstaller
pyinstaller --onefile --windowed main.py --name "AI-Rename-Sort"
```

The executable will be in the `dist/` directory.
