import base64
import io
from PIL import Image
from src.ocr_client import OCRClient, native_agrees, spatial_text


def test_lossless_encoding_preserves_handwriting_pixels():
    image = Image.new('RGB', (30, 30), 'white')
    image.putpixel((15, 15), (12, 34, 56))
    encoded = OCRClient('unused')._image_to_base64(image)
    decoded = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert decoded.format == 'PNG'
    assert decoded.size == image.size and decoded.getpixel((15, 15)) == (12, 34, 56)


def test_partial_native_text_cannot_replace_complete_image_recognition():
    words = ' '.join(f'word{n}' for n in range(100))
    assert native_agrees(words, words)
    assert not native_agrees(words[:100], words)
    assert not native_agrees(words.replace('word50', 'wrongDOB'), 'entirely different patient')


def test_difficult_page_retries_sharper_and_retains_measured_quality(monkeypatch):
    monkeypatch.setattr('pdf2image.pdf2image.pdfinfo_from_path', lambda _: {'Pages': 1})
    dpis = []
    def render(*args, **kwargs):
        dpis.append(kwargs['dpi'])
        return [Image.new('L', (20, 20), 128)]
    monkeypatch.setattr('src.ocr_client.convert_from_path', render)
    monkeypatch.setattr('src.ocr_client.native_page_text', lambda *args: '')
    client = OCRClient('unused')
    replies = iter([{'success': True, 'text': 'Misread', 'word_confidence': .5}, {'success': True, 'text': 'Readable service date', 'word_confidence': .96}])
    client._extract_text_from_image = lambda *args, **kwargs: next(replies)
    result = client.extract_text('fictional.pdf')
    assert dpis == [300, 400]
    assert 'Readable service date' in result['text']
    assert result['page_results'][0]['word_confidence'] == .96
    assert result['confidence_kind'] == 'page_coverage'


def test_verified_white_blank_does_not_hide_nonblank_empty_page(monkeypatch):
    monkeypatch.setattr('pdf2image.pdf2image.pdfinfo_from_path', lambda _: {'Pages': 1})
    monkeypatch.setattr('src.ocr_client.convert_from_path', lambda *args, **kwargs: [Image.new('L', (20, 20), 255)])
    client = OCRClient('unused')
    client._extract_text_from_image = lambda *args, **kwargs: {'success': True, 'text': ''}
    result = client.extract_text('fictional.pdf')
    assert result['page_results'][0]['status'] == 'blank'
    assert len(result['page_results'][0]['attempts']) == 1


def test_separate_header_blocks_rejoin_date_label_and_value():
    def word(text, x, y):
        return {'boundingBox': {'vertices': [{'x': x, 'y': y}, {'x': x+len(text)*8, 'y': y}, {'x': x+len(text)*8, 'y': y+20}, {'x': x, 'y': y+20}]}, 'symbols': [{'text': c} for c in text]}
    pages = [{'height': 1000, 'blocks': [
        {'paragraphs': [{'words': [word('Patient:', 10, 20), word('Alex', 83, 20)]}]},
        {'paragraphs': [{'words': [word('DOE:', 400, 20)]}]},
        {'paragraphs': [{'words': [word('02/02/2026', 440, 20)]}]},
    ]}]
    text = spatial_text(pages)
    assert text == 'Patient: Alex\nDOE: 02/02/2026'
    from src.medical_evidence import supports_encounter_date
    assert supports_encounter_date('DOE: 02/02/2026', '02/02/2026', text)
