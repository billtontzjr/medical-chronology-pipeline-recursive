"""Conservative blank-page assessment of full 300-DPI rendered pages."""


def assess_blank(image):
    gray = image.convert('L')
    width, height = gray.size
    histogram = gray.histogram()
    ink = sum(histogram[:250])
    result = {'method': 'isolated-speckles-300-v1', 'blank': False,
              'ink_pixels': ink, 'width': width, 'height': height}
    if ink == 0:
        return {**result, 'blank': True}
    # This is deliberately limited to nearly white, full-resolution pages.
    # Even one connected mark larger than about two typographic points stays
    # unresolved. Low density alone never establishes that a page is blank.
    if min(width, height) < 1500 or ink / (width * height) > .0001:
        return result
    remaining = {i for i, value in enumerate(gray.tobytes()) if value < 250}
    largest = 0
    while remaining:
        start = remaining.pop()
        todo = [start]
        min_y, min_x = divmod(start, width)
        max_y, max_x = min_y, min_x
        while todo:
            point = todo.pop()
            y, x = divmod(point, width)
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            if max(max_x - min_x + 1, max_y - min_y + 1) > 8:
                return result
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                neighbor = point + dx + dy * width
                if 0 <= x + dx < width and 0 <= y + dy < height and neighbor in remaining:
                    remaining.remove(neighbor)
                    todo.append(neighbor)
        largest = max(largest, max_x - min_x + 1, max_y - min_y + 1)
    return {**result, 'blank': True, 'largest_mark_pixels': largest}
