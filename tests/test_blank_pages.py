from PIL import Image, ImageDraw
import pytest
from src.blank_pages import assess_blank


def test_isolated_scanner_dust_can_be_distinguished_from_writing():
    image = Image.new('L', (2550, 3300), 255)
    draw = ImageDraw.Draw(image)
    for x, y in [(200, 300), (1000, 1400), (1800, 2100)]:
        draw.rectangle((x, y, x + 3, y + 6), fill=90)
    assert assess_blank(image)['blank']


@pytest.mark.parametrize('gray', [0, 180, 248])
def test_even_one_small_faint_written_mark_stays_unresolved(gray):
    image = Image.new('L', (2550, 3300), 255)
    ImageDraw.Draw(image).line((100, 100, 108, 124), fill=gray, width=1)
    assert not assess_blank(image)['blank']


def test_low_resolution_or_dense_speckles_are_not_automatically_blank():
    small = Image.new('L', (500, 600), 255)
    small.putpixel((100, 100), 0)
    assert not assess_blank(small)['blank']
    dense = Image.new('L', (2550, 3300), 255)
    draw = ImageDraw.Draw(dense)
    for x in range(100, 2400, 10):
        for y in range(100, 3200, 10):
            draw.point((x, y), fill=0)
    assert not assess_blank(dense)['blank']
