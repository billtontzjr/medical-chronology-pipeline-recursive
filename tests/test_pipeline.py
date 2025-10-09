"""Unit tests for the medical chronology pipeline."""

import pytest
import os
from pathlib import Path
from src.hooks.formatting_guard import check_formatting_violations


class TestFormattingGuard:
    """Test formatting validation rules."""

    def test_detect_bold_text(self):
        """Test detection of bold text."""
        content = "This has **bold text** in it"
        violations = check_formatting_violations(content)
        assert len(violations) > 0
        assert any('bold' in v.lower() for v in violations)

    def test_detect_bullet_points(self):
        """Test detection of bullet points."""
        content = """
        Some text
        * Bullet point
        More text
        """
        violations = check_formatting_violations(content)
        assert len(violations) > 0
        assert any('bullet' in v.lower() for v in violations)

    def test_detect_numbered_lists(self):
        """Test detection of numbered lists."""
        content = """
        Introduction
        1. First item
        2. Second item
        """
        violations = check_formatting_violations(content)
        assert len(violations) > 0

    def test_detect_narrative_phrasing(self):
        """Test detection of narrative phrasing."""
        content = "The patient was seen for back pain"
        violations = check_formatting_violations(content)
        assert len(violations) > 0
        assert any('narrative' in v.lower() for v in violations)

    def test_clean_content(self):
        """Test that clean content passes validation."""
        content = """
        Chief Complaint: Back pain. History of Present Illness: Patient reports
        pain following motor vehicle collision. Physical Examination: Tenderness
        in lumbar spine. Assessment: Lumbar strain. Plan: Physical therapy.
        """
        violations = check_formatting_violations(content)
        assert len(violations) == 0


class TestDropboxTool:
    """Test Dropbox integration (requires valid token)."""

    @pytest.mark.skipif(
        not os.getenv('DROPBOX_ACCESS_TOKEN'),
        reason="Requires DROPBOX_ACCESS_TOKEN"
    )
    def test_dropbox_connection(self):
        """Test Dropbox connection."""
        from src.tools.dropbox_tool import DropboxTool

        tool = DropboxTool(os.getenv('DROPBOX_ACCESS_TOKEN'))
        # Just test that we can initialize without error
        assert tool.dbx is not None


class TestOCRClient:
    """Test OCR client (requires valid API key)."""

    @pytest.mark.skipif(
        not os.getenv('GOOGLE_CLOUD_API_KEY'),
        reason="Requires GOOGLE_CLOUD_API_KEY"
    )
    def test_ocr_initialization(self):
        """Test OCR client initialization."""
        from src.ocr_client import OCRClient

        client = OCRClient(os.getenv('GOOGLE_CLOUD_API_KEY'))
        assert client.api_key is not None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
