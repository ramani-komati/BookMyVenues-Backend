"""
One spelling for places customers see (QA audit 2026-10-30: cards read
"Hanamakonda", "Hanamkonda" and "Hanumakonda" for the same town).

The town is shown as "Hanamkonda", its common English spelling; the
DISTRICT keeps its official name "Hanumakonda" (venues/data/india_regions.json).
Only the displayed caption changes — what the vendor saved is never
rewritten. Search treats every spelling as the same place
(events/location.place_terms).
"""
import re

# variant -> display spelling (whole words, any case)
DISPLAY_SPELLINGS = (
    (re.compile(r'\b(?:hanamakonda|hanumakonda|hanmakonda|hanamkonda)\b', re.I), 'Hanamkonda'),
)


def display_place(text):
    """`text` with known place-name variants in their one display spelling."""
    if not isinstance(text, str) or not text:
        return text
    for pattern, spelling in DISPLAY_SPELLINGS:
        text = pattern.sub(spelling, text)
    return text
