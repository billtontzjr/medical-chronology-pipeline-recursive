"""Persist page-level extraction coverage without confusing it with accuracy."""
import json
import re
from pathlib import Path

from .deposition_evidence import atomic_json


def coverage_path(pdf, input_dir, extracted_dir):
    return Path(extracted_dir) / Path(pdf).relative_to(input_dir).with_suffix('.ocr.json')


def save_coverage(result, pdf, input_dir, extracted_dir):
    pages = result.get('page_results', [])
    total = result.get('page_count')
    report = {'source_file': str(Path(pdf).relative_to(input_dir)), 'total_pages': total,
              'pages': pages, 'text_pages': sum(p['status'] == 'text' for p in pages),
              'no_text_pages': [p['page'] for p in pages if p['status'] == 'no_text'],
              'error_pages': [p['page'] for p in pages if p['status'] == 'error']}
    report['technical_failure'] = bool(report['error_pages'] or not total or len(pages) != total)
    report['needs_review'] = bool(report['technical_failure'] or report['no_text_pages'])
    atomic_json(coverage_path(pdf, input_dir, extracted_dir), report)
    return report


def collect_coverage(input_dir, extracted_dir):
    input_dir, extracted_dir = Path(input_dir), Path(extracted_dir)
    reports = []
    for pdf in sorted(p for p in input_dir.rglob('*') if p.suffix.lower() == '.pdf'):
        sidecar = coverage_path(pdf, input_dir, extracted_dir)
        if sidecar.exists():
            report = json.loads(sidecar.read_text())
        else:
            txt = extracted_dir / pdf.relative_to(input_dir).with_suffix('.txt')
            content = txt.read_text(encoding='utf-8') if txt.exists() else ''
            pages = sorted(set(map(int, re.findall(r'=== SOURCE PDF PAGE (\d+) ===', content))))
            report = {'source_file': str(pdf.relative_to(input_dir)), 'total_pages': None,
                      'text_pages': len(pages), 'observed_text_pages': pages,
                      'coverage_status': 'unknown_legacy' if content.strip() else 'missing',
                      'technical_failure': not bool(content.strip()), 'needs_review': True}
        reports.append(report)
    return {'note': 'Text extraction coverage is not a check of medical accuracy. Pages with no '
                    'text may be blank, image-only, or unreadable and require source review. '
                    'Older runs have no saved page totals; their coverage is unknown.',
            'files': reports, 'files_needing_review': sum(r['needs_review'] for r in reports),
            'technical_failures': sum(r['technical_failure'] for r in reports)}
