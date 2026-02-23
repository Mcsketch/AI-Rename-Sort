"""Entry point for AI Rename & Sort."""
import tkinter as tk

from src.app import AIRenameSortApp


def main():
    root = tk.Tk()
    app = AIRenameSortApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
