import logging
from pathlib import Path
import pytest
from src.chronology_agent import ChronologyAgent
from src.session_state import SessionStore


def make_agent():
    a = ChronologyAgent.__new__(ChronologyAgent)
    a.logger = logging.getLogger('coverage-test')
    return a


def test_missing_sources_never_pass(tmp_path):
    p=tmp_path/'draft.md';p.write_text('MEDICAL RECORDS SUMMARY\nTest\n\nUndated statement.')
    result=make_agent().verify_chronology(str(p),str(tmp_path/'sources'))
    assert not result['success']
    assert result['review_status']=='incomplete'


def test_distinct_same_day_entries_preserved():
    a='09/17/2024. Clinic. Jane Smith, MD. Office visit. Assessment: Pain.'
    b='09/17/2024. Clinic. Jane Smith, MD. Procedure note. Plan: Procedure performed.'
    assert make_agent()._deduplicate_entries([a,b,a])==[a,b]


def test_full_chunk_sent_to_reviewer():
    a=make_agent();prompts=[]
    a._call_api_with_retry=lambda prompt,**kw: prompts.append(prompt) or 'No issues found.'
    a._verify_entry_batch(['09/17/2024. Entry'],[{'filename':'source','content':'x'*16000+'Critical ending'}])
    assert 'Critical ending' in prompts[0]


def test_mixed_clean_phrase_does_not_hide_issue(tmp_path):
    p=tmp_path/'draft.md';p.write_text('09/17/2024. Clinic. Office visit.')
    a=make_agent()
    a._read_extracted_files=lambda _: [{'filename':'note','content':'09/17/2024 note'}]
    a._verify_entry_batch=lambda *_:'No issues found in the date. Critical: Unsupported treatment.'
    result=a.verify_chronology(str(p),str(tmp_path))
    assert 'Unsupported treatment' in result['verification']
    assert result['review_status']=='human_review_required'


def test_undated_and_range_entries_reported_unreviewed(tmp_path):
    p=tmp_path/'draft.md';p.write_text('Undated entry.\n\n03/01/2024 to 03/15/2024. Therapy.')
    a=make_agent();a._read_extracted_files=lambda _: [{'filename':'note','content':'03/01/2024'}]
    result=a.verify_chronology(str(p),str(tmp_path))
    assert result['entries_unreviewed']==2
    assert result['entries_reviewed']==0
    assert result['documents_checked']==0
    assert result['review_status']=='incomplete'


def test_all_source_groups_reviewed(tmp_path):
    p=tmp_path/'draft.md';p.write_text('09/17/2024. Clinic. Visit.')
    a=make_agent();groups=[]
    a._read_extracted_files=lambda _: [{'filename':str(i),'content':'09/17/2024 '+'x'*19990} for i in range(7)]
    a._verify_entry_batch=lambda entries,docs: groups.extend(d['filename'] for d in docs) or 'No issues found.'
    result=a.verify_chronology(str(p),str(tmp_path))
    assert groups==[str(i) for i in range(7)]
    assert result['documents_checked']==7
    assert 'All entries verified' not in result['verification']


def test_required_disk_rejects_ephemeral_path(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSION_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('REQUIRE_PERSISTENT_STORAGE','true')
    monkeypatch.setattr('os.path.ismount',lambda p: False)
    with pytest.raises(RuntimeError,match='mounted volume'):
        SessionStore(str(tmp_path))


def test_session_survives_store_recreation(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSION_DATA_DIR',str(tmp_path/'disk'))
    monkeypatch.setenv('REQUIRE_PERSISTENT_STORAGE','true')
    monkeypatch.setattr('os.path.ismount',lambda p: True)
    store=SessionStore(str(tmp_path/'application'))
    store.create(session_id='p_20260101_120000',patient_id='p',dropbox_link='',destination_folder='/out')
    assert SessionStore(str(tmp_path/'new_application')).load('p_20260101_120000').patient_id=='p'
