"""Compare a single support gate with the frozen six-question reviewer on fictional text."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.jev_client import JevClient
from src.jev_review import review_flags
from src.deposition_evidence import atomic_json
from scripts.jev_triage_fixtures import FIXTURES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, default=Path('/tmp/jev-triage-comparison.json'))
    args = parser.parse_args()
    baseline = json.loads((Path(__file__).resolve().parents[1] / 'docs/jev-evaluation/questions-v3.json').read_text())
    candidate = {'factual_support': baseline['factual_support']}
    fixture_bytes = json.dumps(FIXTURES, sort_keys=True).encode()
    result = dict(synthetic_only=True, clinical_validation=False, threshold=.9,
                  fixtures_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
                  candidate_questions=candidate, baseline_questions=baseline,
                  acceptance={'unsupported_without_flags_max':0, 'supported_flagged_max':1}, results=[])
    assert len(FIXTURES) == 20 and len({f[0] for f in FIXTURES}) == 20
    if not args.live:
        print('Validated 20 fresh fictional fixtures and frozen candidate/baseline schemas. No API calls.')
        return 0
    from dotenv import load_dotenv
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    client = JevClient()
    result['model'] = client.model
    # Save the predeclared configuration before any request.
    atomic_json(args.output, result)
    for identifier, source, claim, expected in FIXTURES:
        state = {'entry':claim, 'source_pages':[{'document_id':'fictional','page':1,'text':source}]}
        row = dict(id=identifier, expected=expected)
        for name, schema in [('candidate', candidate), ('baseline', baseline)]:
            response = client.evaluate(state, schema)
            row[name] = dict(response=response, flags=review_flags(response['answers']))
        result['results'].append(row)
        atomic_json(args.output, result)
    for name in ['candidate','baseline']:
        rows = result['results']
        metrics = dict(total=len(rows), supported=10, unsupported=10,
            labels_correct=sum(r[name]['response']['answers']['factual_support']['choice']==r['expected'] for r in rows),
            supported_flagged=sum(bool(r[name]['flags']) for r in rows if r['expected']=='supported'),
            unsupported_without_flags=sum(not r[name]['flags'] for r in rows if r['expected']!='supported'))
        metrics['gate_passed'] = metrics['supported_flagged'] <= 1 and metrics['unsupported_without_flags'] == 0
        result[name] = metrics
    atomic_json(args.output, result)
    print(json.dumps({name:result[name] for name in ['candidate','baseline']}))
    return 0 if result['candidate']['gate_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
