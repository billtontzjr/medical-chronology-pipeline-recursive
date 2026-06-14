"""Dropbox integration tool for downloading medical records."""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import unquote, urlparse
import dropbox
from dropbox.files import FileMetadata, FolderMetadata
from dropbox.sharing import SharedLinkMetadata


logger = logging.getLogger(__name__)


def dropbox_url_to_path(value: str) -> str:
    """Convert a Dropbox browser URL to an API path, or return the input as-is.

    Handles:
    - Direct API paths: ``/Team Folder/Patients/Darelyn`` → returned unchanged
    - Home URLs: ``https://www.dropbox.com/home/Team%20Folder/Patients/Darelyn``
      → ``/Team Folder/Patients/Darelyn``
    - Shared links (``.../scl/fo/...``) → returned unchanged so callers can
      still feed them to the shared-link APIs if desired.

    Empty input returns an empty string.
    """
    if not value:
        return ""
    s = value.strip()
    if s.startswith("/"):
        return s
    if not (s.startswith("http://") or s.startswith("https://")):
        return s
    parsed = urlparse(s)
    # Home URLs look like: https://www.dropbox.com/home/<Top Folder>/<subpath>
    # where <Top Folder> is a real Dropbox folder (team folder, personal
    # root folder, etc) — NOT a username. Strip the `/home` prefix and
    # return everything after, URL-decoded.
    path = parsed.path.rstrip("/")
    if path == "/home":
        return "/"
    if path.startswith("/home/"):
        remaining = path[len("/home/"):]
        return unquote("/" + remaining) if remaining else "/"
    # Shared links or anything else — return as-is; not usable for direct upload
    return s


def normalize_dropbox_folder(path: str) -> str:
    """Clean a user-provided Dropbox folder path.

    - Runs the input through :func:`dropbox_url_to_path` first so home URLs
      become paths automatically.
    - Strips surrounding whitespace
    - Ensures a leading ``/``
    - Removes trailing ``/``
    - Collapses duplicate slashes
    """
    if not path:
        return ""
    p = dropbox_url_to_path(path).strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    # Collapse //
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _dropbox_content_hash(path: str) -> str:
    """Compute Dropbox's content hash (SHA256 over 4 MiB blocks, then SHA256 of the concat).

    Used to verify uploads match the local file byte-for-byte, not just by size.
    """
    block_size = 4 * 1024 * 1024
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            hasher.update(hashlib.sha256(chunk).digest())
    return hasher.hexdigest()


def _safe_local_path(local_dir: str, relative_path: str) -> str:
    """Build a local path under local_dir from a Dropbox relative path."""
    base = Path(local_dir).resolve()
    clean_parts = [
        part
        for part in relative_path.replace("\\", "/").split("/")
        if part and part not in {".", ".."}
    ]
    if not clean_parts:
        raise ValueError("Empty Dropbox file path")
    target = base.joinpath(*clean_parts)
    resolved_parent = target.parent.resolve()
    if resolved_parent != base and base not in resolved_parent.parents:
        raise ValueError(f"Refusing to write outside download directory: {relative_path}")
    return str(target)


def _dropbox_relative_path(base_folder: str, entry_path: Optional[str], name: str) -> str:
    """Return entry_path relative to base_folder, falling back to name."""
    if not entry_path:
        return name
    base = (base_folder or "").rstrip("/")
    path = entry_path.lstrip("/")
    if base:
        base = base.lstrip("/")
        prefix = base + "/"
        if path.lower().startswith(prefix.lower()):
            return path[len(prefix):]
    return name


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

    def download_folder(
        self,
        folder_path: str,
        local_dir: str,
        extensions: Optional[List[str]] = None,
        recursive: bool = True,
        _base_folder: Optional[str] = None,
    ) -> Dict:
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
        if _base_folder is None:
            _base_folder = folder_path

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

                        # Download file, preserving subfolders to avoid
                        # collisions between files with the same name.
                        rel_path = _dropbox_relative_path(
                            _base_folder, entry.path_display, entry.name
                        )
                        local_path = _safe_local_path(local_dir, rel_path)
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
                            recursive=True,
                            _base_folder=_base_folder,
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
                    local_path = _safe_local_path(local_dir, link_metadata.name)

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

            # If it's a folder, list and download files RECURSIVELY
            else:
                def list_shared_folder(path):
                    """List all entries for a shared-link folder path."""
                    response = self.dbx.files_list_folder(
                        path,
                        shared_link=dropbox.files.SharedLink(url=shared_link)
                    )
                    entries = list(response.entries)
                    while response.has_more:
                        response = self.dbx.files_list_folder_continue(response.cursor)
                        entries.extend(response.entries)
                    return entries

                # Process all entries (files and folders) recursively
                def process_entries(entries_list, path_prefix=''):
                    """Recursively process files and folders from shared link."""
                    for entry in entries_list:
                        # Process subfolders recursively
                        if isinstance(entry, FolderMetadata):
                            import logging
                            logging.info(f"Entering subfolder: {entry.name}")
                            
                            # List subfolder contents
                            subfolder_path = f"{path_prefix}/{entry.name}" if path_prefix else f"/{entry.name}"
                            try:
                                # Recursively process subfolder entries
                                process_entries(
                                    list_shared_folder(subfolder_path), subfolder_path
                                )
                            except Exception as e:
                                import logging
                                logging.error(f"Error listing subfolder {entry.name}: {e}")

                        # Process files
                        elif isinstance(entry, FileMetadata):
                            if any(entry.name.lower().endswith(ext.lower())
                                  for ext in extensions):
                                try:
                                    # Download via shared link
                                    file_path = f"{path_prefix}/{entry.name}" if path_prefix else f"/{entry.name}"
                                    local_path = _safe_local_path(
                                        local_dir, file_path.lstrip("/")
                                    )
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
                                    import logging
                                    logging.error(f"Failed to download {entry.name}: {type(e).__name__}: {str(e)}")
                                    results['failed'].append({
                                        'name': entry.name,
                                        'error': f"{type(e).__name__}: {str(e)}"
                                    })
                                    results['success'] = False
                            else:
                                results['skipped'].append(entry.name)
                
                # Start processing from root
                process_entries(list_shared_folder(''))

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

    def ensure_folder(self, dropbox_folder: str) -> None:
        """Create the destination folder (and parents) in Dropbox if missing.

        ``files_create_folder_v2`` creates intermediate folders automatically.
        A ``path/conflict/folder`` error means the folder already exists —
        that's fine.
        """
        dropbox_folder = normalize_dropbox_folder(dropbox_folder)
        if not dropbox_folder or dropbox_folder == "/":
            return
        try:
            self.dbx.files_create_folder_v2(dropbox_folder)
        except dropbox.exceptions.ApiError as e:
            if "path/conflict/folder" in str(e):
                return
            # Some team environments may reject nested creation in one call;
            # retry creating parents first.
            parent = os.path.dirname(dropbox_folder)
            if parent and parent != "/" and parent != dropbox_folder:
                self.ensure_folder(parent)
                try:
                    self.dbx.files_create_folder_v2(dropbox_folder)
                    return
                except dropbox.exceptions.ApiError as e2:
                    if "path/conflict/folder" in str(e2):
                        return
                    raise
            raise

    def upload_file(
        self,
        local_path: str,
        dropbox_path: str,
        *,
        max_retries: int = 5,
        verify: bool = True,
    ) -> Dict:
        """Upload one file with retry and optional content-hash verification.

        Args:
            local_path: Path to local file.
            dropbox_path: Destination path in Dropbox (must start with ``/``).
            max_retries: Number of upload attempts on transient errors.
            verify: When True, compare Dropbox's content hash to the local
                file's content hash after upload and fail if they differ.

        Returns:
            ``{'success': bool, ...}``. On failure, includes ``error``.
        """
        if not os.path.exists(local_path):
            return {
                "success": False,
                "error": f"Local file not found: {local_path}",
                "local_path": local_path,
                "dropbox_path": dropbox_path,
            }

        local_size = os.path.getsize(local_path)
        local_hash = _dropbox_content_hash(local_path) if verify else None

        last_error: Optional[str] = None
        for attempt in range(1, max_retries + 1):
            try:
                with open(local_path, "rb") as f:
                    file_data = f.read()

                metadata = self.dbx.files_upload(
                    file_data,
                    dropbox_path,
                    mode=dropbox.files.WriteMode.overwrite,
                    mute=True,
                )

                # Verify via size + content hash
                if metadata.size != local_size:
                    last_error = (
                        f"Size mismatch after upload: local={local_size}, "
                        f"remote={metadata.size}"
                    )
                    logger.warning("%s — retry %d/%d", last_error, attempt, max_retries)
                elif verify and local_hash and getattr(metadata, "content_hash", None):
                    if metadata.content_hash != local_hash:
                        last_error = "Content hash mismatch after upload"
                        logger.warning("%s — retry %d/%d", last_error, attempt, max_retries)
                    else:
                        return {
                            "success": True,
                            "dropbox_path": dropbox_path,
                            "size": metadata.size,
                            "name": metadata.name,
                            "verified": True,
                        }
                else:
                    # Size matched and hash verification unavailable/disabled
                    return {
                        "success": True,
                        "dropbox_path": dropbox_path,
                        "size": metadata.size,
                        "name": metadata.name,
                        "verified": False,
                    }
            except dropbox.exceptions.ApiError as e:
                last_error = f"Dropbox API error: {e}"
                logger.warning("%s — retry %d/%d", last_error, attempt, max_retries)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning("%s — retry %d/%d", last_error, attempt, max_retries)

            # Exponential backoff with jitter
            if attempt < max_retries:
                delay = min(2 ** attempt, 30) + (time.time() % 1)
                time.sleep(delay)

        return {
            "success": False,
            "error": last_error or "Upload failed for unknown reason",
            "local_path": local_path,
            "dropbox_path": dropbox_path,
        }

    def upload_folder(
        self,
        local_dir: str,
        dropbox_folder: str,
        extensions: Optional[List[str]] = None,
        *,
        max_retries: int = 5,
        verify: bool = True,
    ) -> Dict:
        """Upload every file in ``local_dir`` to ``dropbox_folder``.

        Creates intermediate Dropbox folders as needed. Each file is uploaded
        with retry + content-hash verification (see :meth:`upload_file`).
        """
        dropbox_folder = normalize_dropbox_folder(dropbox_folder)

        results = {
            "success": True,
            "uploaded": [],
            "failed": [],
            "skipped": [],
            "destination": dropbox_folder,
        }

        try:
            self.ensure_folder(dropbox_folder)

            local_path_obj = Path(local_dir)
            if not local_path_obj.exists():
                results["success"] = False
                results["error"] = f"Local directory not found: {local_dir}"
                return results

            for file_path in sorted(local_path_obj.iterdir()):
                if not file_path.is_file():
                    continue
                if extensions and not any(
                    file_path.name.lower().endswith(ext.lower()) for ext in extensions
                ):
                    results["skipped"].append(file_path.name)
                    continue

                dropbox_file_path = f"{dropbox_folder}/{file_path.name}"
                upload_result = self.upload_file(
                    str(file_path),
                    dropbox_file_path,
                    max_retries=max_retries,
                    verify=verify,
                )
                if upload_result["success"]:
                    results["uploaded"].append(upload_result)
                else:
                    results["failed"].append(upload_result)
                    results["success"] = False

        except Exception as e:
            results["success"] = False
            results["error"] = str(e)

        return results
