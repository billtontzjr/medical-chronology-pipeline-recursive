"""Render one physical original PDF page without relying on a browser PDF plugin."""

import hashlib
from pathlib import Path
import re
import subprocess
import tempfile


def render_source_page(original, page, expected_hash=None):
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 100000:
        raise ValueError("Choose a valid physical PDF page.")
    # An isolated snapshot prevents a source replacement midway through rendering.
    # TemporaryDirectory is private and is removed on success and every failure.
    with tempfile.TemporaryDirectory(prefix="chronology-source-page-") as directory:
        snapshot = Path(directory) / "source.pdf"
        digest = hashlib.sha256()
        with Path(original).open("rb") as source, snapshot.open("wb") as copy:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                copy.write(chunk)
        snapshot.chmod(0o600)
        if expected_hash and digest.hexdigest() != expected_hash:
            raise RuntimeError(
                "The source file changed. Refresh and review its current version."
            )
        try:
            info = subprocess.run(
                ["pdfinfo", str(snapshot)], capture_output=True, timeout=15, check=True
            )
            match = re.search(rb"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
            if not match or page > int(match.group(1)):
                raise ValueError(
                    "That physical page is not present in the original PDF."
                )
            prefix = Path(directory) / "page"
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    "-scale-to",
                    "1600",
                    "-png",
                    str(snapshot),
                    str(prefix),
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            return prefix.with_suffix(".png").read_bytes()
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "The page preview is unavailable. Use Open original to inspect the PDF."
            ) from exc
