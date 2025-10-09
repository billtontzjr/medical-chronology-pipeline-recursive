"""Dropbox integration tool for downloading medical records."""

import os
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import unquote, urlparse
import dropbox
from dropbox.files import FileMetadata, FolderMetadata
from dropbox.sharing import SharedLinkMetadata


class DropboxTool:
    """Handle Dropbox operations for medical record downloads."""

    def __init__(self, access_token: str = None, use_oauth: bool = True):
        """
        Initialize Dropbox client.

        Args:
            access_token: Dropbox API access token (legacy, for backwards compatibility)
            use_oauth: If True, use OAuth2 with refresh tokens (recommended)
        """
        if use_oauth:
            # Use OAuth2 with auto-refresh
            from .dropbox_oauth import get_dropbox_client
            try:
                self.dbx = get_dropbox_client()
            except Exception as e:
                if access_token:
                    # Fallback to legacy token
                    self.dbx = dropbox.Dropbox(access_token)
                else:
                    raise Exception(
                        f"OAuth setup failed: {e}\n"
                        "Please run: python setup_dropbox_oauth.py"
                    )
        else:
            # Legacy: direct token
            if not access_token:
                raise ValueError("access_token required when use_oauth=False")
            self.dbx = dropbox.Dropbox(access_token)

    def _parse_dropbox_url(self, url_or_path: str) -> str:
        """
        Parse a Dropbox URL and convert it to a path.

        Supports:
        - Direct paths: /folder/subfolder
        - Home URLs: https://www.dropbox.com/home/Bill%20tontz/folder
        - Shared links: https://www.dropbox.com/scl/fo/...

        Args:
            url_or_path: URL or path string

        Returns:
            Dropbox path starting with /
        """
        # Already a path
        if url_or_path.startswith('/'):
            return url_or_path

        # Parse URL
        parsed = urlparse(url_or_path)

        # Handle home URLs: https://www.dropbox.com/home/username/path
        if '/home/' in parsed.path:
            # Extract path after /home/username/
            parts = parsed.path.split('/home/')
            if len(parts) > 1:
                # Get everything after the username
                remaining = parts[1]
                # Find the next slash (after username)
                slash_idx = remaining.find('/')
                if slash_idx != -1:
                    path = remaining[slash_idx:]
                    # URL decode (convert %20 to spaces, etc.)
                    return unquote(path)

        # For shared links or other URLs, return as-is
        # (will be handled by the shared link API)
        return url_or_path

    def list_files(self, folder_path: str = "") -> List[Dict]:
        """
        List all files in a Dropbox folder.

        Args:
            folder_path: Path to the folder (empty string for root)

        Returns:
            List of file metadata dictionaries
        """
        results = []

        try:
            response = self.dbx.files_list_folder(folder_path)

            while True:
                for entry in response.entries:
                    if isinstance(entry, FileMetadata):
                        results.append({
                            'name': entry.name,
                            'path': entry.path_display,
                            'size': entry.size,
                            'modified': entry.client_modified
                        })

                if not response.has_more:
                    break

                response = self.dbx.files_list_folder_continue(response.cursor)

        except dropbox.exceptions.ApiError as e:
            raise Exception(f"Failed to list files: {str(e)}")

        return results

    def download_file(self, dropbox_path: str, local_path: str) -> Dict:
        """
        Download a single file from Dropbox.

        Args:
            dropbox_path: Path to file in Dropbox
            local_path: Local path to save the file

        Returns:
            Dictionary with download result
        """
        try:
            # Ensure local directory exists
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)

            # Download file
            metadata, response = self.dbx.files_download(dropbox_path)

            # Save to local file
            with open(local_path, 'wb') as f:
                f.write(response.content)

            return {
                'success': True,
                'local_path': local_path,
                'size': metadata.size,
                'name': metadata.name
            }

        except dropbox.exceptions.ApiError as e:
            return {
                'success': False,
                'error': str(e),
                'dropbox_path': dropbox_path
            }

    def download_folder(self, folder_path: str, local_dir: str,
                       extensions: Optional[List[str]] = None, recursive: bool = True) -> Dict:
        """
        Download all files from a Dropbox folder.

        Args:
            folder_path: Path to folder in Dropbox
            local_dir: Local directory to save files
            extensions: Optional list of file extensions to filter (e.g., ['.pdf'])
            recursive: If True, download from subfolders as well (default: True)

        Returns:
            Dictionary with download results
        """
        if extensions is None:
            extensions = ['.pdf']

        results = {
            'success': True,
            'downloaded': [],
            'failed': [],
            'skipped': []
        }

        try:
            # List all entries in the folder
            response = self.dbx.files_list_folder(folder_path)

            while True:
                for entry in response.entries:
                    # Handle files
                    if isinstance(entry, FileMetadata):
                        # Check if file matches extension filter
                        if not any(entry.name.lower().endswith(ext.lower())
                                  for ext in extensions):
                            results['skipped'].append(entry.name)
                            continue

                        # Download file
                        local_path = os.path.join(local_dir, entry.name)
                        download_result = self.download_file(entry.path_display, local_path)

                        if download_result['success']:
                            results['downloaded'].append(download_result)
                        else:
                            results['failed'].append(download_result)
                            results['success'] = False

                    # Handle subfolders recursively
                    elif recursive and isinstance(entry, FolderMetadata):
                        subfolder_results = self.download_folder(
                            entry.path_display,
                            local_dir,
                            extensions,
                            recursive=True
                        )
                        # Merge results
                        results['downloaded'].extend(subfolder_results['downloaded'])
                        results['failed'].extend(subfolder_results['failed'])
                        results['skipped'].extend(subfolder_results['skipped'])
                        if not subfolder_results['success']:
                            results['success'] = False

                if not response.has_more:
                    break

                response = self.dbx.files_list_folder_continue(response.cursor)

        except Exception as e:
            results['success'] = False
            results['error'] = str(e)

        return results

    def get_shared_link_files(self, shared_link: str, local_dir: str,
                              extensions: Optional[List[str]] = None) -> Dict:
        """
        Download files from a Dropbox shared link OR direct path.

        Args:
            shared_link: Dropbox shared link URL OR direct path (e.g., "/My Folder/Patient")
                        Also supports home URLs like: https://www.dropbox.com/home/username/path
            local_dir: Local directory to save files
            extensions: Optional list of file extensions to filter

        Returns:
            Dictionary with download results
        """
        # Parse URL and convert to path if needed
        parsed_path = self._parse_dropbox_url(shared_link)

        # Check if this is a direct path (starts with /) instead of a URL
        if parsed_path.startswith('/'):
            return self.download_folder(parsed_path, local_dir, extensions)

        # Use the parsed result for shared links
        shared_link = parsed_path
        if extensions is None:
            extensions = ['.pdf']

        results = {
            'success': True,
            'downloaded': [],
            'failed': [],
            'skipped': []
        }

        try:
            # Get shared link metadata
            link_metadata = self.dbx.sharing_get_shared_link_metadata(shared_link)

            # If it's a file, download directly
            if isinstance(link_metadata, dropbox.files.FileMetadata):
                if any(link_metadata.name.lower().endswith(ext.lower())
                      for ext in extensions):
                    local_path = os.path.join(local_dir, link_metadata.name)

                    # Download via shared link
                    _, response = self.dbx.sharing_get_shared_link_file(shared_link)

                    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(local_path, 'wb') as f:
                        f.write(response.content)

                    results['downloaded'].append({
                        'success': True,
                        'local_path': local_path,
                        'name': link_metadata.name
                    })
                else:
                    results['skipped'].append(link_metadata.name)

            # If it's a folder, list and download files
            else:
                # List folder contents
                list_result = self.dbx.files_list_folder(
                    '',
                    shared_link=dropbox.files.SharedLink(url=shared_link)
                )

                for entry in list_result.entries:
                    # Skip folders
                    if isinstance(entry, FolderMetadata):
                        continue

                    if isinstance(entry, FileMetadata):
                        if any(entry.name.lower().endswith(ext.lower())
                              for ext in extensions):
                            local_path = os.path.join(local_dir, entry.name)

                            try:
                                # Download via shared link and path
                                # Use filename with leading slash if path_display is None
                                file_path = entry.path_display if entry.path_display else f"/{entry.name}"

                                _, response = self.dbx.sharing_get_shared_link_file(
                                    shared_link,
                                    path=file_path
                                )

                                Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                                with open(local_path, 'wb') as f:
                                    f.write(response.content)

                                results['downloaded'].append({
                                    'success': True,
                                    'local_path': local_path,
                                    'name': entry.name
                                })
                            except Exception as e:
                                results['failed'].append({
                                    'name': entry.name,
                                    'error': str(e)
                                })
                                results['success'] = False
                        else:
                            results['skipped'].append(entry.name)

        except dropbox.exceptions.ApiError as e:
            results['success'] = False
            results['error'] = f"Dropbox API error: {str(e)}"
        except Exception as e:
            results['success'] = False
            results['error'] = str(e)

        # If some files failed but we didn't hit a global error, add summary
        if not results['success'] and 'error' not in results and results['failed']:
            failed_files = ', '.join([f['name'] for f in results['failed']])
            results['error'] = f"Failed to download {len(results['failed'])} files: {failed_files}"

        return results
