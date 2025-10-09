"""Dropbox OAuth2 helper with auto-refresh tokens."""

import os
import requests
from datetime import datetime, timedelta
from typing import Optional


class DropboxOAuth:
    """Handle Dropbox OAuth2 authentication with refresh tokens."""

    def __init__(self, app_key: str, app_secret: str, refresh_token: str):
        """
        Initialize OAuth handler.

        Args:
            app_key: Dropbox app key
            app_secret: Dropbox app secret
            refresh_token: Permanent refresh token
        """
        self.app_key = app_key
        self.app_secret = app_secret
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    def get_access_token(self) -> str:
        """
        Get a valid access token, refreshing if necessary.

        Returns:
            Valid access token
        """
        # Check if we have a token and it's still valid
        if self._access_token and self._token_expiry:
            if datetime.now() < self._token_expiry:
                return self._access_token

        # Refresh the token
        return self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        """
        Refresh the access token using the refresh token.

        Returns:
            New access token

        Raises:
            Exception: If refresh fails
        """
        token_url = 'https://api.dropbox.com/oauth2/token'
        token_data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.app_key,
            'client_secret': self.app_secret
        }

        response = requests.post(token_url, data=token_data)

        if response.status_code != 200:
            raise Exception(f"Failed to refresh token: {response.text}")

        token_info = response.json()
        self._access_token = token_info['access_token']

        # Tokens expire in 4 hours, we'll refresh after 3.5 hours to be safe
        expires_in = token_info.get('expires_in', 14400)  # Default 4 hours
        self._token_expiry = datetime.now() + timedelta(seconds=expires_in - 1800)

        return self._access_token


def get_dropbox_client(app_key: str = None, app_secret: str = None, refresh_token: str = None):
    """
    Create a Dropbox client with OAuth2 authentication.

    Args:
        app_key: Dropbox app key (defaults to env var)
        app_secret: Dropbox app secret (defaults to env var)
        refresh_token: Refresh token (defaults to env var)

    Returns:
        Dropbox client instance
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

    # Create OAuth handler
    oauth = DropboxOAuth(app_key, app_secret, refresh_token)

    # Get access token
    access_token = oauth.get_access_token()

    # Create Dropbox client
    return dropbox.Dropbox(access_token)
