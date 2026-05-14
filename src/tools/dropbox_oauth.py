"""Dropbox OAuth2 helper with auto-refresh tokens."""

import os
from typing import Optional


def get_dropbox_client(app_key: str = None, app_secret: str = None, refresh_token: str = None):
    """
    Create a Dropbox client with OAuth2 authentication and auto-refresh.

    Args:
        app_key: Dropbox app key (defaults to env var)
        app_secret: Dropbox app secret (defaults to env var)
        refresh_token: Refresh token (defaults to env var)

    Returns:
        Dropbox client instance with automatic token refresh
    """
    import dropbox
    from dotenv import load_dotenv

    # Explicitly load environment variables
    load_dotenv()

    app_key = app_key or os.getenv('DROPBOX_APP_KEY')
    app_secret = app_secret or os.getenv('DROPBOX_APP_SECRET')
    refresh_token = refresh_token or os.getenv('DROPBOX_REFRESH_TOKEN')

    if not all([app_key, app_secret, refresh_token]):
        raise Exception(
            "Missing Dropbox OAuth credentials. "
            "Run setup_dropbox_oauth.py to configure."
        )

    # Create Dropbox client with built-in auto-refresh
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret
    )
  
