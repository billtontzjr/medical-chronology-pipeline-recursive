"""Recover the saved model from source fingerprints, never from a page default."""
import hashlib
import json
from pathlib import Path

from .deposition_evidence import atomic_json
from .session_lock import session_lock


GENERATION_VERSION = 'page-bound-diagnostics-v5'
LEGACY_VERSIONS = ('source-bound-diagnostics-v4', 'same-day-care-v3')


def batch_signature(batches, exclusions, model, *, version=GENERATION_VERSION):
    return hashlib.sha256(json.dumps({'version': version,
        'source_exclusions': exclusions, 'model': model, 'batches': batches},
        sort_keys=True).encode()).hexdigest()


def save_model_config(batches_dir, model):
    """Pin once in the batches directory; never replace a different run model."""
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError('Saved run model is invalid; review before resuming.')
    folder = Path(batches_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with session_lock(folder / '.run_model.lock'):
        path = folder / 'run_model.json'
        if path.exists():
            if json.loads(path.read_text()).get('model') != model:
                raise ValueError('This run already has a different saved model; existing metadata was preserved.')
        else:
            atomic_json(path, {'model': model})


def saved_model(agent, input_dir, batches_dir, candidates, *, persist=True):
    folder = Path(batches_dir)
    manifest_path = folder / 'batch_manifest.json'
    model_path = folder / 'run_model.json'
    if model_path.exists():
        model = json.loads(model_path.read_text()).get('model')
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError('Saved run model is invalid; review before resuming.')
        return model
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        documents = agent._read_extracted_files(str(input_dir))
        batches = agent._plan_batches(documents)
        exclusions = getattr(agent, '_source_exclusions', [])
        choices = list(candidates) + [manifest.get('model'), agent.model]
        matches = {model for model in choices if isinstance(model, str) and model
                   and any(batch_signature(batches, exclusions, model, version=version) == manifest.get('signature')
                           for version in (GENERATION_VERSION,) + LEGACY_VERSIONS)}
        legacy_reader = getattr(agent, '_read_legacy_extracted_files', None)
        if not matches and callable(legacy_reader):
            # Only model recovery may reproduce the whitespace-flattened layout.
            # Generation always fingerprints the new page-preserving documents.
            legacy_documents = legacy_reader(str(input_dir))
            legacy_batches = agent._plan_batches(legacy_documents)
            legacy_exclusions = getattr(agent, '_source_exclusions', [])
            matches = {model for model in choices if isinstance(model, str) and model
                       and any(batch_signature(legacy_batches, legacy_exclusions, model, version=version)
                               == manifest.get('signature') for version in LEGACY_VERSIONS)}
        if len(matches) != 1:
            raise ValueError('The saved model and source fingerprint could not be confirmed. '
                             'Existing batches were preserved; review before resuming.')
        model = matches.pop()
    elif any(folder.glob('batch_*.md')):
        raise ValueError('Saved batches lack their source fingerprint; review before resuming.')
    else:
        model = agent.model
    if persist:
        save_model_config(folder, model)
    return model
