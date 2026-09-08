"""Inventory first, checkpoint each verified file, then reconcile the remote tree."""
import hashlib
from pathlib import Path

from dropbox.files import FileMetadata, FolderMetadata, SharedLink
from dropbox.sharing import FileLinkMetadata
from .deposition_evidence import atomic_json
from .output_safety import OUTPUT_ROOT


def download_snapshot(tool, source, directory, progress=lambda _: None):
    # Import here to avoid a module cycle.
    from .tools.dropbox_tool import dropbox_url_to_path, _safe_local_path, _dropbox_content_hash
    source = dropbox_url_to_path(source)
    shared = not source.startswith('/')
    metadata = tool.dbx.sharing_get_shared_link_metadata(source) if shared else None
    source_path = getattr(metadata, 'path_lower', None) if shared else source
    if source_path and (source_path.rstrip('/').casefold() == OUTPUT_ROOT.casefold() or
                        source_path.casefold().startswith(OUTPUT_ROOT.casefold() + '/')):
        raise ValueError('Choose original source records outside the app output folder.')
    single = isinstance(metadata, (FileLinkMetadata, FileMetadata))
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)

    def inventory():
        found = []
        def add(entry, relative, remote):
            if not relative.lower().endswith('.pdf'):
                return
            _safe_local_path(str(root), relative)
            found.append({'path': relative, 'remote': remote,
                          'size': getattr(entry, 'size', None),
                          'content_hash': getattr(entry, 'content_hash', None),
                          'rev': getattr(entry, 'rev', None)})
        def walk(remote='', relative=''):
            response = tool.dbx.files_list_folder(remote, **({'shared_link': SharedLink(url=source)} if shared else {}))
            while True:
                for entry in response.entries:
                    rel = f'{relative}/{entry.name}'.lstrip('/')
                    path = f'{remote.rstrip("/")}/{entry.name}'
                    if isinstance(entry, FolderMetadata):
                        # Never ingest prior app outputs when given a parent folder.
                        if path.casefold() == OUTPUT_ROOT.casefold():
                            raise ValueError('Source contains the app output tree. Choose a specific records folder.')
                        walk(path, rel)
                    elif isinstance(entry, FileMetadata):
                        add(entry, rel, path)
                if not response.has_more:
                    break
                response = tool.dbx.files_list_folder_continue(response.cursor)
        if single:
            current = tool.dbx.sharing_get_shared_link_metadata(source)
            add(current, current.name, '')
        else:
            walk('' if shared else source.rstrip('/'))
        found.sort(key=lambda f: f['path'])
        if not found:
            raise ValueError('No PDF files found in the selected source.')
        if len({f['path'].casefold() for f in found}) != len(found):
            raise ValueError('Source inventory contains colliding file paths.')
        return found

    manifest = inventory()  # Any failed folder/page listing stops before download.
    atomic_json(root / 'source-inventory.json', {'complete': False, 'files': manifest})
    downloaded = []
    for number, item in enumerate(manifest, 1):
        progress(f'Downloading source file {number}/{len(manifest)}')
        target = Path(_safe_local_path(str(root), item['path']))
        expected_hash, expected_size = item['content_hash'], item['size']
        cached = (target.is_file() and expected_hash and
                  target.stat().st_size == expected_size and
                  _dropbox_content_hash(str(target)) == expected_hash)
        if not cached:
            if shared:
                _, response = tool.dbx.sharing_get_shared_link_file(source, **({'path': item['remote']} if not single else {}))
            else:
                _, response = tool.dbx.files_download(item['remote'])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + '.part')
            temporary.write_bytes(response.content)
            if not temporary.stat().st_size or (expected_size is not None and temporary.stat().st_size != expected_size):
                raise RuntimeError('Downloaded PDF size differs from the source inventory. Resume to retry.')
            if expected_hash and _dropbox_content_hash(str(temporary)) != expected_hash:
                raise RuntimeError('Downloaded PDF hash differs from the source inventory. Resume to retry.')
            temporary.replace(target)
        downloaded.append(dict(item, size=target.stat().st_size,
                               sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
    if inventory() != manifest:
        raise RuntimeError('Source records changed during download. Resume to reconcile the new inventory.')
    atomic_json(root / 'source-inventory.json', {'complete': True, 'files': downloaded})
    return downloaded
