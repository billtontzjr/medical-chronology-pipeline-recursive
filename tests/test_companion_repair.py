import json
import pytest
from src.companion_reports import generate_reports
from src.medical_evidence import EvidenceReview


@pytest.mark.parametrize('repair_supported', [True, False])
def test_failed_overview_has_one_source_checked_repair_and_durable_replay(tmp_path, repair_supported):
    entries = [{'id': 'fictional', 'text': 'Pain decreased from 6/10 to 3/10.', 'service_dates': ['02/05/2026']}]
    calls = []
    def call(prompt, **kwargs):
        calls.append(prompt)
        if prompt.startswith('Prepare an executive'):
            repaired = 'Repair the rejected draft' in prompt
            if repaired:
                assert 'elevated pain is unsupported' in prompt
                assert 'Pain decreased from 6/10 to 3/10.' in prompt
            return json.dumps({'summary': 'Pain decreased.' if repaired else 'Pain elevated.', 'gaps': 'No candidate gaps.', 'entry_ids': ['fictional']})
        payload = json.loads(prompt.split('\n', 1)[1])
        repaired = payload['companion']['summary'] == 'Pain decreased.'
        return json.dumps({'supported': repaired and repair_supported, 'complete': True, 'reason': 'Matches source.' if repaired and repair_supported else 'elevated pain is unsupported'})
    def run():
        if repair_supported:
            assert generate_reports(entries, call, tmp_path)['summary.md'] == 'Pain decreased.'
        else:
            with pytest.raises(EvidenceReview, match='unsupported'):
                generate_reports(entries, call, tmp_path)
    run()
    assert len(calls) == 4
    saved = {p.name: p.read_bytes() for p in tmp_path.glob('*.json')}
    assert len(saved) == 4
    assert 'Pain elevated.' in (tmp_path / 'overview-1.json').read_text()
    run()
    assert len(calls) == 4
    assert saved == {p.name: p.read_bytes() for p in tmp_path.glob('*.json')}
    assert entries[0]['text'] == 'Pain decreased from 6/10 to 3/10.'
