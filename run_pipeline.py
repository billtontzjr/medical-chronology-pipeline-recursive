"""Headless CLI for precision-chronology.

Useful for batch processing or CI smoke tests where the Streamlit UI is
not desired. The CLI loads ``.env``, validates required environment
variables, instantiates the pipeline, and invokes ``run`` synchronously
(``asyncio.run``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

import click
from dotenv import load_dotenv

from src.cross_checker import CROSS_CHECK_JACCARD_THRESHOLD
from src.pipeline import PrecisionChronologyPipeline


REQUIRED_ENV = (
    "ANTHROPIC_API_KEY",
    "GOOGLE_VISION_API_KEY",
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
)


def _validate_env() -> None:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        click.echo(
            "Missing required environment variables: " + ", ".join(missing),
            err=True,
        )
        sys.exit(1)


def _progress(msg: str) -> None:
    click.echo(msg)


@click.command()
@click.option(
    "--dropbox-link",
    required=False,
    help="Dropbox shared link containing the source PDFs.",
)
@click.option(
    "--patient-id",
    default=None,
    help="Optional patient ID; used as a session-id prefix.",
)
@click.option(
    "--destination",
    default=None,
    help="Dropbox destination folder for outputs. Defaults to the pipeline's default folder.",
)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume an existing session by session_id (skips create_session).",
)
@click.option(
    "--model-extraction",
    default=None,
    help="Claude model id for the extraction stage (default: claude-sonnet-4-6).",
)
@click.option(
    "--model-assembly",
    default=None,
    help="Claude model id for the assembly stage (default: claude-sonnet-4-6).",
)
@click.option(
    "--strict-cross-check",
    is_flag=True,
    default=False,
    help="Fail the pipeline when any phrase falls below the Jaccard threshold.",
)
@click.option(
    "--cross-check-threshold",
    default=CROSS_CHECK_JACCARD_THRESHOLD,
    show_default=True,
    type=float,
    help="Jaccard threshold for the cross-check pass.",
)
@click.option(
    "--base-dir",
    default=None,
    help="Override the working directory root (sessions live in <base-dir>/data/sessions/).",
)
def main(
    dropbox_link: Optional[str],
    patient_id: Optional[str],
    destination: Optional[str],
    resume_session_id: Optional[str],
    model_extraction: Optional[str],
    model_assembly: Optional[str],
    strict_cross_check: bool,
    cross_check_threshold: float,
    base_dir: Optional[str],
) -> None:
    """Run the precision-chronology pipeline headlessly."""
    load_dotenv()
    _validate_env()

    pipeline = PrecisionChronologyPipeline(
        google_api_key=os.environ.get("GOOGLE_VISION_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        dropbox_token=os.environ.get("DROPBOX_ACCESS_TOKEN"),
        base_dir=base_dir,
        model_extraction=model_extraction,
        model_assembly=model_assembly,
    )

    if resume_session_id:
        session_id = resume_session_id
        click.echo(f"Resuming session: {session_id}")
    else:
        if not dropbox_link:
            click.echo("--dropbox-link is required for new sessions.", err=True)
            sys.exit(1)
        state = pipeline.create_session(
            dropbox_link=dropbox_link,
            patient_id=patient_id,
            destination_folder=destination,
        )
        session_id = state.session_id
        click.echo(f"Created session: {session_id}")

    result = asyncio.run(
        pipeline.run(
            session_id,
            progress_callback=_progress,
            model_extraction=model_extraction,
            model_assembly=model_assembly,
            strict_cross_check=strict_cross_check,
            cross_check_threshold=cross_check_threshold,
        )
    )

    click.echo("")
    click.echo(json.dumps(result, indent=2))
    if result.get("status") != "complete":
        sys.exit(2)


if __name__ == "__main__":
    main()
