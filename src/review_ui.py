"""Persistent team review controls; decisions use the locked pipeline methods."""
import json
from pathlib import Path

import streamlit as st

from .deposition_review import review_items, parse_ranges
from .manual_review import collect_manual_reviews
from .ocr_review import ocr_items


def review_count(store, state):
    sources = {str(Path(x['report']['source_file']).with_suffix('')) for x in ocr_items(store, state)}
    for path in store.batches_dir(state.session_id).glob('batch_*.deposition-work.json'):
        work = json.loads(path.read_text())
        if work.get('blocked_stage'):
            sources.add(str(Path(work['source_file']).with_suffix('')))
    sources.update(str(Path(x['source_file']).with_suffix('')) for x in collect_manual_reviews(store.batches_dir(state.session_id)))
    return len(sources) if sources else state.phases['header'].data.get('manual_review_count', 0)


def ranges_text(refs):
    return '; '.join(str(a) if a == b else f'{a}-{b}' for a, b in refs
                     if isinstance(a, int) and isinstance(b, int))


def render_review_panel(pipeline, state, key_prefix, resume):
    count = review_count(pipeline.store, state)
    if not count:
        return
    st.warning(f'Needs review: {count} document(s). Unresolved items remain flagged even when a draft completes.')
    coverage_count = len(ocr_items(pipeline.store, state))
    withheld_count = state.phases['header'].data.get('manual_review_count', 0)
    if coverage_count or withheld_count:
        st.caption(f'{coverage_count} document(s) have OCR coverage flags. The saved draft has {withheld_count} withheld source section(s). These counts may overlap.')
    panel_key = f'review_open_{key_prefix}_{state.session_id}'
    if st.button(f'Review documents ({count})', key=panel_key + '_button'):
        st.session_state[panel_key] = not st.session_state.get(panel_key, False)
    if not st.session_state.get(panel_key, False):
        return
    st.caption('Your named decisions are saved with the source version and time. Deferral withholds the whole document; it does not verify it. Original records remain unchanged.')
    try:
        items = review_items(pipeline.store.extracted_dir(state.session_id), pipeline.store.batches_dir(state.session_id))
    except (ValueError, OSError, KeyError) as exc:
        st.error(f'The deposition source needs inspection before a decision can be saved: {exc}')
        items = []
    for item in items:
        decision = item['decision'] or {}
        blocked = item['work'].get('blocked_stage')
        if not blocked and decision.get('status') != 'deferred':
            continue
        key = panel_key + '_' + item['batch']
        with st.expander('Deposition review: ' + item['document']['filename'], expanded=True):
            st.write('Review needed: ' + ('Witness identity and deposition date' if blocked == 'identity' else str(blocked or 'Deferred document')))
            if decision:
                st.info(f"Last decision: {decision['status'].replace('_', ' ')} by {decision['reviewer']} on {decision['reviewed_at']}. {decision['reason']}")
            rejections = item['work'].get('rejections', [])
            if rejections:
                st.write('Evidence check: ' + rejections[-1]['error'])
            st.caption('These are extracted source lines, not original PDF page/line numbers. Use the source PDF to confirm identity and case association.')
            source = pipeline.store.input_dir(state.session_id) / Path(item['document']['filename']).with_suffix('.pdf')
            root = pipeline.store.input_dir(state.session_id).resolve()
            if (source.resolve().is_relative_to(root) and source.is_file()
                    and st.checkbox('Show original deposition download', key=key+'_show_pdf')):
                st.download_button('Download original deposition for review', source.read_bytes(), file_name=source.name,
                                   mime='application/pdf', key=key+'_pdf')
            proposed = decision.get('identity') or item['proposal'] or {}
            for field, label in [('date', 'Deposition date'), ('witness', 'Witness name'), ('credentials', 'Credentials')]:
                st.write(f"Proposed {label.lower()}: {proposed.get(field) or 'Not supplied'}")
                refs = proposed.get(field+'_refs', [])
                if refs:
                    try:
                        quotes, _ = item['index'].resolve(refs, (0, 20000))
                        for quote in quotes:
                            st.text(quote)
                    except ValueError:
                        st.warning('The proposed citation is invalid. Select supporting lines from the source below.')
            with st.expander('Read source text with evidence line numbers'):
                st.text(item['index'].numbered(0, 20000))
            with st.form(key+'_form'):
                reviewer = st.text_input('Reviewer name', key=key+'_reviewer')
                note = st.text_area('Decision note', key=key+'_note')
                metadata = {}
                if blocked == 'identity':
                    for field, label in [('date', 'Deposition date (MM/DD/YYYY)'), ('witness', 'Witness name'), ('credentials', 'Credentials (leave blank if none)')]:
                        metadata[field] = st.text_input(label, value=str(proposed.get(field) or ''), key=key+'_'+field)
                        refs = proposed.get(field+'_refs') or []
                        try:
                            default = ranges_text(refs)
                        except (ValueError, TypeError):
                            default = ''
                        metadata[field+'_ranges'] = st.text_input(label + ' — supporting source lines', value=default,
                            help='Use the L-numbered source lines above, such as 27; 84-85. Each cited passage must support the value.', key=key+'_'+field+'_refs')
                    confirmed = st.checkbox('I reviewed the source and confirm this witness, deposition date, and association with this case.', key=key+'_confirmed')
                    approve = st.form_submit_button('Confirm identity and continue')
                else:
                    confirmed, approve = False, False
                    st.caption('This issue concerns testimony, not just identity. Identity confirmation cannot approve unsupported statements. Defer the document or provide a corrected source in a refreshed run.')
                defer = st.form_submit_button('Defer this document and continue')
            if approve or defer:
                try:
                    if approve:
                        for field in ('date', 'witness', 'credentials'):
                            metadata[field+'_refs'] = parse_ranges(metadata.pop(field+'_ranges'))
                    pipeline.review_deposition(state.session_id, item['batch'], fingerprint=item['fingerprint'],
                        action='approved_identity' if approve else 'deferred', reviewer=reviewer,
                        reason=note, metadata=metadata if approve else None, confirmed=confirmed)
                    resume(pipeline, state.session_id, key_prefix=key)
                    st.rerun()
                except (ValueError, RuntimeError, OSError) as exc:
                    st.error(str(exc))
    for item in ocr_items(pipeline.store, state):
        report, decision = item['report'], item['decision'] or {}
        name = report['source_file']
        key = panel_key + '_ocr_' + name
        with st.expander('OCR review: ' + name, expanded=True):
            st.write(f"Coverage: {report.get('coverage_status', 'page review required')}. Total pages: {report.get('total_pages') or 'Unknown'}.")
            st.write('Failed pages: ' + (', '.join(map(str, report.get('error_pages', []))) or 'None listed'))
            st.write('Pages without readable text: ' + (', '.join(map(str, report.get('no_text_pages', []))) or 'Unknown or none listed'))
            st.caption('A page without extracted text may be blank, image-only, or unreadable. This panel cannot certify it as medically reviewed.')
            if decision:
                st.info(f"Last decision: {decision['status'].replace('_', ' ')} by {decision['reviewer']} on {decision['reviewed_at']}. {decision['reason']}")
            source = pipeline.store.input_dir(state.session_id) / name
            if (source.resolve().is_relative_to(pipeline.store.input_dir(state.session_id).resolve()) and source.is_file()
                    and st.checkbox('Show original file download', key=key+'_show_pdf')):
                st.download_button('Download original file for review', source.read_bytes(), file_name=source.name, mime='application/pdf', key=key+'_pdf')
            st.caption('Deferring or retrying this source preserves the old draft and rebuilds generation because source composition can change. Other completed OCR files are retained. For a replacement source, update the source folder and start a refreshed run.')
            st.caption('Retry applies to this file. Other unresolved extraction problems may still require separate review decisions before generation continues.')
            with st.form(key+'_form'):
                reviewer = st.text_input('Reviewer name', key=key+'_reviewer')
                note = st.text_area('Decision note', key=key+'_note')
                defer = st.form_submit_button('Defer this file and continue')
                retry = st.form_submit_button('Retry extraction and continue')
            if defer or retry:
                try:
                    pipeline.review_ocr(state.session_id, name, fingerprint=item['fingerprint'],
                        action='deferred' if defer else 'retry_requested', reviewer=reviewer, reason=note)
                    resume(pipeline, state.session_id, key_prefix=key)
                    st.rerun()
                except (ValueError, RuntimeError, OSError) as exc:
                    st.error(str(exc))
