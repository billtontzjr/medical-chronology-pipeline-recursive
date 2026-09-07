import json
import pytest
from src.encounters import (ConsolidationReviewRequired, consolidate,
                            group_entries, parse_entry)


def entry(provider='Alex Example, MD', service='Office Visit', body='Neck pain.'):
    return f'05/22/2025. Clinic. {provider}. Visit Type: {service}. History: {body}'


def response(text, count=2):
    return json.dumps({'text': text, 'covered_ids': list(range(1, count+1)),
                      'coverage': [{'input_id': i, 'disposition': 'merged',
                                    'retained_detail': 'Clinical findings and procedure retained.'}
                                   for i in range(1, count+1)]})


@pytest.mark.parametrize('provider', ['Alex Example, M.D.', 'Alex Example, D.O.', 'Dr. Alex Example'])
def test_normalizes_credentials(provider):
    assert parse_entry(entry(provider))['provider'] == 'alex example'


def test_middle_initial_is_part_of_identity():
    assert parse_entry(entry('Alex J. Example, MD'))['provider'] == 'alex j example'


def test_merge_candidates_preserve_different_providers_and_deposition():
    entries = [entry(), entry(service='Lumbar Block'), entry('Robin Other, MD'),
               '05/22/2025. Alex Example, Deposition. The witness described pain.',
               entry('Provider not documented'), entry('Provider not documented')]
    assert group_entries(entries) == [[0, 1], [2], [3], [4], [5]]


def test_surname_alias_requires_unique_match():
    assert group_entries([entry(), entry('Dr. Example')]) == [[0, 1]]
    assert group_entries([entry(), entry('Robin Example, MD'), entry('Dr. Example')]) == [[0], [1], [2]]


def test_cross_batch_merge_preserves_distinct_procedures_and_caches(tmp_path):
    inputs = [entry(service='Evaluation and Cervical Ablation', body='C3–C6; neck pain 4/10.'),
              entry(service='Lumbar Ablation', body='L2–3 through L5–S1; 80°C for 90 seconds.')]
    combined = entry(service='Evaluation and Cervical/Lumbar Ablation',
                     body='Neck pain 4/10. Cervical C3–C6 and lumbar L2–3 through L5–S1 ablations used 80°C for 90 seconds.')
    calls = []
    def api(prompt, **kwargs):
        calls.append(prompt)
        return response(combined)
    result, trail = consolidate(inputs, api, tmp_path/'merge.json', 'test')
    assert len(result) == 1 and 'Visit Type:' not in result[0]
    assert all(x in result[0] for x in ['C3–C6', 'L2–3', 'L5–S1', '80°C', '90 seconds'])
    assert trail[0]['inputs'] == [x.replace('Visit Type: ', '') for x in inputs]
    assert consolidate(inputs, api, tmp_path/'merge.json', 'test')[0] == result
    assert len(calls) == 1
    consolidate(inputs, api, tmp_path/'merge.json', 'different-model')
    assert len(calls) == 2


@pytest.mark.parametrize('invalid', [response(entry(), 1), response(entry('Robin Other, MD')),
                                   response(entry().replace('05/22/2025', '05/23/2025')), 'not json'])
def test_invalid_merge_stops_assembly(invalid, tmp_path):
    with pytest.raises(ConsolidationReviewRequired):
        consolidate([entry(), entry(service='Block')], lambda *a, **kw: invalid, tmp_path/'merge.json')
    assert not (tmp_path/'merge.json').exists()


def test_actual_batch_assembly_merges_across_files(tmp_path):
    from src.chronology_agent import ChronologyAgent
    import logging
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger = logging.getLogger('merge-test')
    a.model = 'test'
    (tmp_path/'batch_001.md').write_text(entry(service='Office Evaluation'))
    (tmp_path/'batch_002.md').write_text(entry(service='Lumbar Block', body='Block performed.'))
    combined = entry(service='Office Evaluation and Lumbar Block', body='Neck pain. Block performed.')
    a._call_api_with_retry = lambda *args, **kw: response(combined)
    result = a._combine_batches(str(tmp_path))
    assert result == combined.replace('Visit Type: ', '')
    assert len(a._encounter_merges) == 1
    assert (tmp_path/'encounter_consolidation.json').exists()
