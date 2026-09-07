"""Recover the saved model from source fingerprints, never from a page default."""
import hashlib
import json
from pathlib import Path

from .deposition_evidence import atomic_json


def batch_signature(batches, exclusions, model):
    return hashlib.sha256(json.dumps({'version': 'same-day-care-v3',
        'source_exclusions': exclusions, 'model': model, 'batches': batches},
        sort_keys=True).encode()).hexdigest()


def saved_model(agent, input_dir, batches_dir, candidates):
    folder = Path(batches_dir)
    manifest_path = folder / 'batch_manifest.json'
    model_path = folder / 'run_model.json'
    if model_path.exists():
        model = json.loads(model_path.read_text()).get('model')
        if not isinstance(model, str) or not model:
            raise ValueError('Saved run model is invalid; review before resuming.')
        return model
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        documents = agent._read_extracted_files(str(input_dir))
        batches = agent._plan_batches(documents)
        exclusions = getattr(agent, '_source_exclusions', [])
        choices = list(candidates) + [manifest.get('model'), agent.model]
        matches = {model for model in choices if isinstance(model, str) and model
                   and batch_signature(batches, exclusions, model) == manifest.get('signature')}
        if len(matches) != 1:
            raise ValueError('The saved model and source fingerprint could not be confirmed. '
                             'Existing batches were preserved; review before resuming.')
        model = matches.pop()
    elif any(folder.glob('batch_*.md')):
        raise ValueError('Saved batches lack their source fingerprint; review before resuming.')
    else:
        model = agent.model
    atomic_json(model_path, {'model': model})
    return model
