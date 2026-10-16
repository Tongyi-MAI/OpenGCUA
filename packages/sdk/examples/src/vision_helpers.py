"""Small screenshot-only OCR grounder for the coordinate demo, not a general VLM.

Input is image bytes; no AX nodes, resource IDs, or fixed button coordinates.
Requires the local tesseract executable. Rejects absent/ambiguous text matches.
"""
import csv
import io
import subprocess
from PIL import Image


def locate_text(png: bytes, label: str) -> dict:
    # This demo uses light text on a dark Android theme. Threshold pixels
    # to separate lettering from button backgrounds; preserve image dimensions.
    image = Image.open(io.BytesIO(png)).convert("L")
    binary = image.point(lambda value: 0 if value > 170 else 255)
    encoded = io.BytesIO(); binary.save(encoded, format="PNG")
    result = subprocess.run(
        ['tesseract', 'stdin', 'stdout', '--psm', '11', '-l', 'eng', 'tsv'],
        input=encoded.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=1.5, check=True,
    )
    rows = csv.DictReader(io.StringIO(result.stdout.decode()), delimiter='\t')
    lines = {}
    for row in rows:
        if row.get('level') != '5' or not row['text'].strip():
            continue
        key = (row['page_num'], row['block_num'], row['par_num'], row['line_num'])
        lines.setdefault(key, []).append(row)
    expected = label.casefold().split()
    matches = []
    for words in lines.values():
        for start in range(len(words)-len(expected)+1):
            group = words[start:start+len(expected)]
            if [w['text'].casefold() for w in group] != expected:
                continue
            if min(float(w['conf']) for w in group) < 70:
                continue
            left = min(int(w['left']) for w in group)
            top = min(int(w['top']) for w in group)
            right = max(int(w['left'])+int(w['width']) for w in group)
            bottom = max(int(w['top'])+int(w['height']) for w in group)
            matches.append({'text':label,'bounds':[left,top,right,bottom],
                            'point':[(left+right)/2,(top+bottom)/2],
                            'confidence':min(float(w['conf']) for w in group)})
    if len(matches) != 1:
        raise ValueError(f'Expected one visual text match for {label!r}, found {len(matches)}')
    return matches[0]
