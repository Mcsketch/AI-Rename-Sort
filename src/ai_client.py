"""LMStudio API client (OpenAI-compatible local API)."""
import json
import re

import requests


MAX_CONTENT_PREVIEW = 8000

# Keywords in model IDs that indicate vision capability
_VISION_KEYWORDS = (
    "vision", "-vl", "llava", "moondream", "cogvlm", "phi-vision",
    "qwen-vl", "minicpm-v", "internvl", "deepseek-vl", "yi-vl",
    "bakllava", "obsidian", "pixtral", "gemma-3",
)


class LMStudioClient:
    """Client for the LMStudio local inference server."""

    def __init__(self, base_url="http://localhost:1234"):
        self.base_url = base_url.rstrip("/")
        # Debug: last payload sent and raw response received
        self._last_messages: list = []
        self._last_raw_response: str = ""

    # ------------------------------------------------------------------
    # Connection / model listing
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Model classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def classify_model(model_id: str) -> str:
        """Classify a model ID as ``'vision'`` or ``'text'`` by keyword match."""
        lower = model_id.lower()
        for kw in _VISION_KEYWORDS:
            if kw in lower:
                return "vision"
        return "text"

    @classmethod
    def auto_assign_models(cls, models: list[str]) -> dict[str, str]:
        """Pick the best vision and text models from a list of model IDs.

        Returns ``{"vision_model": ..., "text_model": ...}`` with empty
        strings when no suitable model is found.
        """
        vision = ""
        text = ""
        for mid in models:
            kind = cls.classify_model(mid)
            if kind == "vision" and not vision:
                vision = mid
            elif kind == "text" and not text:
                text = mid
        # If only one type was found, use it for both slots as a fallback
        if not vision and text:
            vision = ""
        if not text and vision:
            text = ""
        return {"vision_model": vision, "text_model": text}

    # ------------------------------------------------------------------
    # Chat helper
    # ------------------------------------------------------------------

    def chat(self, model, messages, max_tokens=1024):
        """Send a chat completion request and return the assistant reply."""
        self._last_messages = messages
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
        result = data["choices"][0]["message"]["content"]
        self._last_raw_response = result
        return result

    # ------------------------------------------------------------------
    # File-type-specific prompt builders
    # ------------------------------------------------------------------

    @staticmethod
    def _folders_str(available_folders: list[str]) -> str:
        if available_folders:
            return "\n".join(f"- {f}" for f in available_folders)
        return "- Other"

    @staticmethod
    def _json_schema_reminder() -> str:
        return (
            'Respond ONLY with valid JSON in this exact format:\n'
            '{"filename": "suggested_name", "folder": "folder/path", '
            '"reason": "brief explanation"}'
        )

    def _build_image_messages(self, file_content, filename, folders_str):
        """Build messages for an image file (vision model)."""
        system = (
            "You are a meticulous image analyst and file-naming assistant.\n"
            "Your job is to produce a specific, information-rich filename based "
            "entirely on what you actually observe in the image — NOT the original filename.\n\n"
            "STEP 1 — READ ALL TEXT: Scan every part of the image for text: titles, "
            "headings, labels, dates, names, addresses, invoice/receipt numbers, "
            "prices, brand names, watermarks, logos. Transcribe them mentally.\n"
            "STEP 2 — IDENTIFY THE SUBJECT: What is the main subject? "
            "(person, document, product, scene, screenshot, receipt, ID card, etc.)\n"
            "STEP 3 — BUILD A SPECIFIC FILENAME using the most identifying information found:\n"
            "  Good examples: receipt_amazon_47_32_2024_03_15, john_smith_passport_photo, "
            "grand_canyon_sunrise_2023, invoice_acme_corp_INV8821, "
            "drivers_license_front_john_doe, xray_chest_2024_01_20\n"
            "  Bad examples: image_001, photo, scan, document, picture\n"
            "  Rules: lowercase, underscores instead of spaces, no extension, max 60 chars.\n"
            "STEP 4 — PICK the most appropriate folder.\n\n"
            "CRITICAL: If you can read a date, name, number, or company from the image, "
            "USE IT in the filename. Never guess or reuse the original filename.\n\n"
            + self._json_schema_reminder()
        )
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Original filename (ignore for naming — use content only): {filename}\n\n"
                            f"Available folders:\n{folders_str}\n\n"
                            "Examine every detail of this image. Read all visible text first, "
                            "identify the subject, then produce a specific filename and folder."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": file_content},
                    },
                ],
            },
        ]

    def _build_pdf_messages(self, file_content, filename, folders_str):
        """Build messages for a PDF file (text model)."""
        content_preview = str(file_content)[:MAX_CONTENT_PREVIEW] if file_content else "(empty)"
        system = (
            "You are a document analyst and file-naming assistant.\n"
            "You will receive extracted text from a PDF. Your job is to produce a "
            "specific, information-rich filename based on the document content — "
            "NOT the original filename.\n\n"
            "STEP 1 — CLASSIFY the document:\n"
            "  invoice, receipt, bank statement, tax return, W-2, 1099, pay stub, "
            "contract, lease agreement, insurance policy, medical record, lab result, "
            "prescription, birth certificate, passport, driver licence, utility bill, "
            "credit card statement, loan document, deed, will, academic transcript, "
            "research paper, manual, warranty, letter, form, report, etc.\n"
            "STEP 2 — EXTRACT specific identifiers from the text:\n"
            "  - Company or person names (issuer AND recipient if present)\n"
            "  - Dates (YYYY_MM_DD format preferred)\n"
            "  - Reference/invoice/account/policy/case numbers\n"
            "  - Dollar amounts for financial docs\n"
            "  - Tax year for tax documents\n"
            "  - Doctor/hospital name for medical docs\n"
            "STEP 3 — BUILD a filename: [doc_type]_[entity]_[identifier]_[date]\n"
            "  Good: invoice_amazon_INV-48291_2024_03_15, tax_return_federal_2023, "
            "bank_statement_chase_checking_2024_02, w2_acme_corp_2023, "
            "lease_agreement_123_main_st_2024_01, labresult_quest_diagnostics_2024_06_10\n"
            "  Bad: document, scan001, pdf_file, report\n"
            "  Rules: lowercase, underscores, no extension, max 80 chars.\n"
            "STEP 4 — PICK the most appropriate folder.\n\n"
            "CRITICAL: Use actual values from the document. If you see a date, use it. "
            "If you see an invoice number, use it. Never invent data.\n\n"
            + self._json_schema_reminder()
        )
        user = (
            f"Original filename (ignore for naming — use content only): {filename}\n\n"
            f"Available folders:\n{folders_str}\n\n"
            f"Extracted PDF text:\n{content_preview}\n\n"
            "Read the text carefully, classify the document, extract specific identifiers, "
            "then produce a precise filename and folder."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_video_messages(self, file_content, filename, folders_str, is_frame: bool):
        """Build messages for a video file.

        If *is_frame* is True, ``file_content`` is a base64 data-URL of a
        representative frame and should be sent as an image to a vision model.
        Otherwise it is a text description (metadata only).
        """
        system = (
            "You are a video analyst and file-naming assistant.\n"
            "Produce a specific, information-rich filename based on what you observe — "
            "NOT the original filename.\n\n"
            "STEP 1 — READ ALL TEXT on screen: titles, captions, watermarks, "
            "channel names, dates, overlays, lower-thirds.\n"
            "STEP 2 — IDENTIFY the content: tutorial, vlog, gameplay, movie clip, "
            "security footage, family video, event recording, screen recording, "
            "product demo, travel footage, etc.\n"
            "STEP 3 — BUILD a specific filename:\n"
            "  Good: minecraft_survival_let_there_be_fire_ep12, "
            "wedding_reception_smith_jones_2024_06_15, "
            "security_cam_front_door_2024_03_10, "
            "tutorial_react_hooks_useeffect_explained\n"
            "  Bad: video001, clip, movie, recording\n"
            "  Rules: lowercase, underscores, no extension, max 80 chars.\n"
            "STEP 4 — PICK the most appropriate folder.\n\n"
            + self._json_schema_reminder()
        )
        if is_frame and isinstance(file_content, str) and file_content.startswith("data:"):
            return [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Original filename (ignore for naming — use content only): {filename}\n\n"
                                f"Available folders:\n{folders_str}\n\n"
                                "This is a representative frame from a video. Read all on-screen text, "
                                "identify the content, then produce a specific filename and folder."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": file_content},
                        },
                    ],
                },
            ]
        # Metadata-only fallback (text model)
        content_preview = str(file_content)[:MAX_CONTENT_PREVIEW] if file_content else "(no metadata)"
        user = (
            f"Original filename (ignore for naming — use content only): {filename}\n\n"
            f"Available folders:\n{folders_str}\n\n"
            f"Video metadata:\n{content_preview}\n\n"
            "Use the metadata to infer video content and produce a specific filename and folder."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_document_messages(self, file_content, filename, folders_str):
        """Build messages for Office documents (docx, xlsx, pptx, etc.)."""
        content_preview = str(file_content)[:MAX_CONTENT_PREVIEW] if file_content else "(empty)"
        system = (
            "You are a document analyst and file-naming assistant.\n"
            "You will receive extracted text from an Office document (Word, Excel, PowerPoint, etc.). "
            "Produce a specific, information-rich filename from the content — NOT the original filename.\n\n"
            "STEP 1 — CLASSIFY: spreadsheet, budget, invoice, report, memo, letter, "
            "presentation, contract, resume/CV, form, project plan, meeting notes, etc.\n"
            "STEP 2 — EXTRACT identifiers: title, author, company, project name, "
            "date, version number, department, dollar totals, key subject.\n"
            "STEP 3 — BUILD a filename: [doc_type]_[subject_or_entity]_[date_or_version]\n"
            "  Good: budget_q1_2024_marketing_dept, meeting_notes_project_alpha_2024_03_15, "
            "resume_jane_doe_software_engineer, quarterly_report_acme_corp_q3_2023, "
            "invoice_contractor_smith_march_2024\n"
            "  Bad: document1, spreadsheet, report, file\n"
            "  Rules: lowercase, underscores, no extension, max 80 chars.\n"
            "STEP 4 — PICK the most appropriate folder.\n\n"
            + self._json_schema_reminder()
        )
        user = (
            f"Original filename (ignore for naming — use content only): {filename}\n\n"
            f"Available folders:\n{folders_str}\n\n"
            f"Document content:\n{content_preview}\n\n"
            "Classify the document, extract specific identifiers, then produce a precise filename and folder."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_text_messages(self, file_content, filename, folders_str):
        """Build messages for plain-text / code / config files."""
        content_preview = str(file_content)[:MAX_CONTENT_PREVIEW] if file_content else "(empty)"
        system = (
            "You are a code and text analyst and file-naming assistant.\n"
            "Produce a specific, information-rich filename based on the actual content — "
            "NOT the original filename.\n\n"
            "STEP 1 — CLASSIFY: Python/JS/TS/Go/Rust/etc. source file, shell script, "
            "config file, CSV data, JSON/YAML/TOML config, log file, markdown notes, "
            "plain text notes, README, requirements, etc.\n"
            "STEP 2 — EXTRACT the most specific identifiers:\n"
            "  Source code: what does it do? (e.g. auth service, image resizer, "
            "database migration, API client, CLI tool). Class/function names. "
            "Project or module name from imports or comments.\n"
            "  Config: what app/service does it configure? What environment?\n"
            "  Data/CSV: what data does it contain? Column names, date range.\n"
            "  Notes/text: what is the topic? Who is it about? Any dates?\n"
            "STEP 3 — BUILD a specific filename:\n"
            "  Good: user_authentication_service_py, nginx_production_config, "
            "customers_export_2024_03, database_migration_v3_add_users_table, "
            "meeting_notes_2024_03_15_sprint_planning, aws_s3_upload_helper\n"
            "  Bad: script, file1, code, notes, data, config\n"
            "  Rules: lowercase, underscores, no extension, max 80 chars.\n"
            "STEP 4 — PICK the most appropriate folder.\n\n"
            + self._json_schema_reminder()
        )
        user = (
            f"Original filename (ignore for naming — use content only): {filename}\n\n"
            f"Available folders:\n{folders_str}\n\n"
            f"File content:\n{content_preview}\n\n"
            "Read the content carefully, identify exactly what this file is and does, "
            "then produce a precise filename and folder."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_unknown_messages(self, file_content, filename, folders_str):
        """Build messages for unknown file types."""
        content_preview = str(file_content)[:MAX_CONTENT_PREVIEW] if file_content else "(empty)"
        system = (
            "You are a file-naming assistant.\n"
            "You will receive information about a file with an uncommon or unrecognised type. "
            "Use all available clues — file extension, size, and any readable metadata — "
            "to produce the most specific, accurate filename possible.\n\n"
            "Consider: 3D print files (.stl, .3mf, .gcode), CAD files, font files, "
            "database files, archive files, disk images, firmware, design files, etc.\n"
            "Rules: lowercase, underscores instead of spaces, no extension, max 80 chars.\n"
            "Good: benchy_3dbenchy_ender3_gcode, verdana_font_regular, "
            "laser_cut_coaster_design_svg, firmware_router_v2_1\n"
            "Bad: file, unknown, data\n\n"
            + self._json_schema_reminder()
        )
        user = (
            f"Original filename: {filename}\n\n"
            f"Available folders:\n{folders_str}\n\n"
            f"File information:\n{content_preview}\n\n"
            "Use all available clues to infer the file's purpose and produce a specific filename and folder."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ------------------------------------------------------------------
    # Main analysis entry point
    # ------------------------------------------------------------------

    def analyze_file(self, model, file_content, file_type, filename, available_folders):
        """Ask the AI to suggest a filename and folder for the given file.

        Returns a dict with keys: ``filename``, ``folder``, ``reason``.
        """
        folders_str = self._folders_str(available_folders)

        if file_type == "image" and isinstance(file_content, str) and file_content.startswith("data:"):
            messages = self._build_image_messages(file_content, filename, folders_str)
        elif file_type == "pdf":
            messages = self._build_pdf_messages(file_content, filename, folders_str)
        elif file_type == "video":
            is_frame = isinstance(file_content, str) and file_content.startswith("data:")
            messages = self._build_video_messages(file_content, filename, folders_str, is_frame)
        elif file_type == "document":
            messages = self._build_document_messages(file_content, filename, folders_str)
        elif file_type == "text":
            messages = self._build_text_messages(file_content, filename, folders_str)
        else:
            messages = self._build_unknown_messages(file_content, filename, folders_str)

        raw = self.chat(model, messages)
        return self._parse_suggestion(raw, available_folders)

    # ------------------------------------------------------------------
    # Duplicate comparison
    # ------------------------------------------------------------------

    def compare_for_duplicate(
        self,
        model: str,
        content1, type1: str, name1: str,
        content2, type2: str, name2: str,
    ) -> dict:
        """Ask the AI whether two files are duplicates.

        Returns ``{"is_duplicate": bool, "confidence": float, "reason": str}``.
        """
        system = (
            "You are a duplicate-file detection assistant.\n"
            "You will be shown information about two files. Determine whether "
            "they are duplicates or near-duplicates of each other.\n"
            "Consider: same content with different names, resized or "
            "re-encoded copies, documents with identical text but different "
            "formatting, etc.\n\n"
            "Respond ONLY with valid JSON:\n"
            '{"is_duplicate": true/false, "confidence": 0.0-1.0, '
            '"reason": "brief explanation"}'
        )

        # Build content representations for both files
        parts = []
        image_parts = []
        for i, (content, ftype, fname) in enumerate(
            [(content1, type1, name1), (content2, type2, name2)], 1
        ):
            label = f"File {i}"
            if ftype == "image" and isinstance(content, str) and content.startswith("data:"):
                parts.append(f"{label}: {fname} (image)")
                image_parts.append((label, content))
            else:
                preview = str(content)[:MAX_CONTENT_PREVIEW // 2] if content else "(empty)"
                parts.append(f"{label}: {fname} (type: {ftype})\nContent preview:\n{preview}")

        # If both files are images, send as a multimodal message
        if len(image_parts) == 2:
            user_content = [
                {"type": "text", "text": (
                    "Compare these two images and determine if they are "
                    "duplicates or near-duplicates.\n\n"
                    f"File 1: {name1}\nFile 2: {name2}"
                )},
                {"type": "image_url", "image_url": {"url": image_parts[0][1]}},
                {"type": "image_url", "image_url": {"url": image_parts[1][1]}},
            ]
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
        elif len(image_parts) == 1:
            # One image, one text — mixed comparison
            text_info = "\n\n".join(parts)
            user_content = [
                {"type": "text", "text": (
                    "Compare these two files and determine if they are "
                    "duplicates.\n\n" + text_info
                )},
                {"type": "image_url", "image_url": {"url": image_parts[0][1]}},
            ]
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
        else:
            # Both text-based
            text_info = "\n\n".join(parts)
            user_msg = (
                "Compare these two files and determine if they are "
                "duplicates or near-duplicates.\n\n" + text_info
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ]

        raw = self.chat(model, messages, max_tokens=512)
        return self._parse_duplicate_response(raw)

    # ------------------------------------------------------------------
    # Response parsers
    # ------------------------------------------------------------------

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

    @staticmethod
    def _parse_duplicate_response(response: str) -> dict:
        """Extract duplicate-detection JSON from the model response."""
        json_match = re.search(r'\{[^{}]*"is_duplicate"[^{}]*\}', response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return {
                    "is_duplicate": bool(data.get("is_duplicate", False)),
                    "confidence": float(data.get("confidence", 0.0)),
                    "reason": str(data.get("reason", "")),
                }
            except (json.JSONDecodeError, ValueError):
                pass
        return {"is_duplicate": False, "confidence": 0.0, "reason": "Could not parse AI response"}
