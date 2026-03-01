"""Duplicate file detection via hashing and optional perceptual similarity."""
import hashlib
import os
from pathlib import Path

# Re-use the extension sets from file_processor
from .file_processor import IMAGE_EXTENSIONS

HASH_CHUNK_SIZE = 65536  # 64 KB reads for hashing
HAMMING_THRESHOLD = 10   # max Hamming distance for perceptual-hash match
NAME_SIMILARITY_THRESHOLD = 0.65  # difflib ratio threshold


class DuplicateDetector:
    """Find candidate duplicate files across one or more directories."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_candidates(
        self,
        filepath: str,
        search_dirs: list[str],
    ) -> list[tuple[str, str]]:
        """Return a list of ``(candidate_path, match_type)`` tuples.

        *match_type* is one of ``"exact"``, ``"perceptual"``, or
        ``"similar_name"``.

        *search_dirs* are walked (non-recursively per sub-folder already
        present) to find potential matches.
        """
        if not os.path.isfile(filepath):
            return []

        results: list[tuple[str, str]] = []
        target_hash = self._sha256(filepath)
        target_name = Path(filepath).stem.lower()
        target_ext = Path(filepath).suffix.lower()
        is_image = target_ext in IMAGE_EXTENSIONS

        # Gather all files in search dirs (one level of recursion)
        all_files = self._collect_files(search_dirs, filepath)

        # --- Pass 1: exact hash match ---
        for candidate in all_files:
            if self._sha256(candidate) == target_hash:
                results.append((candidate, "exact"))

        if results:
            # Exact duplicates found – no need for further passes
            return results

        # --- Pass 2: perceptual hash for images (optional) ---
        if is_image:
            target_phash = self._perceptual_hash(filepath)
            if target_phash is not None:
                for candidate in all_files:
                    if Path(candidate).suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    cand_phash = self._perceptual_hash(candidate)
                    if cand_phash is not None:
                        distance = target_phash - cand_phash
                        if distance <= HAMMING_THRESHOLD:
                            results.append((candidate, "perceptual"))

        if results:
            return results

        # --- Pass 3: filename similarity (cheap pre-filter) ---
        import difflib

        for candidate in all_files:
            cand_name = Path(candidate).stem.lower()
            ratio = difflib.SequenceMatcher(None, target_name, cand_name).ratio()
            if ratio >= NAME_SIMILARITY_THRESHOLD:
                results.append((candidate, "similar_name"))

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_files(search_dirs: list[str], exclude_path: str) -> list[str]:
        """Collect all file paths in *search_dirs*, excluding *exclude_path*."""
        exclude_norm = os.path.normcase(os.path.abspath(exclude_path))
        files: list[str] = []
        seen: set[str] = set()
        for directory in search_dirs:
            if not os.path.isdir(directory):
                continue
            for root, _dirs, filenames in os.walk(directory):
                for fname in filenames:
                    full = os.path.join(root, fname)
                    norm = os.path.normcase(os.path.abspath(full))
                    if norm == exclude_norm or norm in seen:
                        continue
                    seen.add(norm)
                    files.append(full)
        return files

    @staticmethod
    def _sha256(filepath: str) -> str:
        """Compute the SHA-256 hex digest of a file."""
        h = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            return ""
        return h.hexdigest()

    @staticmethod
    def _perceptual_hash(filepath: str):
        """Return an ``imagehash`` perceptual hash, or *None* if unavailable."""
        try:
            import imagehash
            from PIL import Image

            with Image.open(filepath) as img:
                return imagehash.phash(img)
        except Exception:
            return None
