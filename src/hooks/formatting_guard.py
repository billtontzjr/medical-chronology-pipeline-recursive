"""Formatting guard hook to prevent chronology formatting violations."""

import re
from typing import Dict, Any


def check_formatting_violations(content: str) -> list:
    """
    Check content for formatting violations.

    Args:
        content: The text content to check

    Returns:
        List of violation messages (empty if no violations)
    """
    violations = []

    # Check for bold text (**text**)
    if re.search(r'\*\*[^*]+\*\*', content):
        violations.append("Bold text (**text**) is not allowed in medical chronologies")

    # Check for bullet points
    bullet_patterns = [
        r'^\s*\*\s+',  # * bullet
        r'^\s*-\s+',   # - bullet
        r'^\s*•\s+',   # • bullet
        r'^\d+\.\s+',  # numbered list
    ]
    for pattern in bullet_patterns:
        if re.search(pattern, content, re.MULTILINE):
            violations.append(f"Bullet points or lists are not allowed (found pattern: {pattern})")
            break

    # Check for all-caps sections (5+ consecutive uppercase words)
    if re.search(r'\b[A-Z]{2,}(?:\s+[A-Z]{2,}){4,}\b', content):
        violations.append("All-caps sections are not allowed")

    # Check for narrative phrasing
    narrative_phrases = [
        r'the patient was seen for',
        r'patient presented with a chief complaint',
        r'the patient presented',
        r'pre-procedure laboratory studies were performed',
    ]
    for phrase in narrative_phrases:
        if re.search(phrase, content, re.IGNORECASE):
            violations.append(
                f"Narrative phrasing not allowed: '{phrase}'. "
                "Use direct, factual tone (e.g., 'Chief Complaint:' instead)"
            )

    return violations


def formatting_guard_hook(tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pre-tool-use hook to check formatting before FileWrite operations.

    Args:
        tool_name: Name of the tool being called
        tool_input: Input parameters for the tool

    Returns:
        Dictionary with 'allow' boolean and optional 'message'
    """
    # Only intercept FileWrite operations for chronology files
    if tool_name != 'FileWrite':
        return {'allow': True}

    file_path = tool_input.get('file_path', '')
    content = tool_input.get('content', '')

    # Only check chronology.md files
    if not file_path.endswith('chronology.md'):
        return {'allow': True}

    # Check for violations
    violations = check_formatting_violations(content)

    if violations:
        violation_msg = "\n".join(f"  - {v}" for v in violations)
        return {
            'allow': False,
            'message': f"FORMATTING VIOLATIONS DETECTED:\n{violation_msg}\n\n"
                      "Please fix these issues before writing the chronology file."
        }

    return {'allow': True}


def get_formatting_hooks() -> Dict[str, Any]:
    """
    Get hook configuration for formatting guards.

    Returns:
        Dictionary with hook configuration
    """
    return {
        'preToolUse': [formatting_guard_hook],
        'postToolUse': []
    }
