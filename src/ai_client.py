"""LMStudio API client (OpenAI-compatible local API)."""
import json
import re

import requests


class LMStudioClient:
    """Client for the LMStudio local inference server."""

    def __init__(self, base_url="http://localhost:1234"):
        self.base_url = base_url.rstrip("/")

    def get_models(self):
        """Return list of model IDs available in LMStudio."""
        response = requests.get(f"{self.base_url}/v1/models", timeout=5)
        if response.status_code == 200:
            data = response.json()
            return [m["id"] for m in data.get("data", [])]
        return []

    def is_connected(self):
        """Return True if the LMStudio server is reachable."""
        try:
            response = requests.get(f"{self.base_url}/v1/models", timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def chat(self, model, messages, max_tokens=512):
        """Send a chat completion request and return the assistant reply."""
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        response = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def analyze_file(self, model, file_content, file_type, filename, available_folders):
        """Ask the AI to suggest a filename and folder for the given file.

        Returns a dict with keys: ``filename``, ``folder``, ``reason``.
        """
        folders_str = (
            "\n".join(f"- {f}" for f in available_folders)
            if available_folders
            else "- Other"
        )

        system_prompt = (
            "You are a file organisation assistant. "
            "Analyse the provided file content and suggest:\n"
            "1. A descriptive, concise filename (no extension, use underscores instead of spaces).\n"
            "2. The most appropriate folder from the available options.\n\n"
            "Respond ONLY with valid JSON in this exact format:\n"
            '{"filename": "suggested_name", "folder": "folder/path", "reason": "brief explanation"}'
        )

        # Vision request for images (base64 data URL)
        if file_type == "image" and isinstance(file_content, str) and file_content.startswith("data:"):
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Original filename: {filename}\n\n"
                                f"Available folders:\n{folders_str}\n\n"
                                "Please analyse this image and suggest a filename and folder."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": file_content},
                        },
                    ],
                },
            ]
        else:
            content_preview = str(file_content)[:3000] if file_content else "(empty)"
            user_prompt = (
                f"Original filename: {filename}\n"
                f"File type: {file_type}\n\n"
                f"Available folders:\n{folders_str}\n\n"
                f"File content preview:\n{content_preview}\n\n"
                "Please suggest a filename and folder for this file."
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        raw = self.chat(model, messages)
        return self._parse_suggestion(raw, available_folders)

    def _parse_suggestion(self, response, available_folders):
        """Extract filename/folder JSON from the model response."""
        json_match = re.search(r'\{[^{}]*"filename"[^{}]*\}', response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                filename = re.sub(r'[<>:"/\\|?*]', "_", data.get("filename", "unnamed_file")).strip()
                folder = data.get("folder", available_folders[0] if available_folders else "Other")
                reason = data.get("reason", "")
                return {"filename": filename, "folder": folder, "reason": reason}
            except json.JSONDecodeError:
                pass
        return {
            "filename": "unnamed_file",
            "folder": available_folders[0] if available_folders else "Other",
            "reason": "Could not parse AI response",
        }
