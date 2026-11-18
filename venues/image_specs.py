"""
Image standards for listing photos — what vendors and admins may upload
for venue photos, service images and event photos, checked on every upload.

Mirrors the customer site's src/utils/imageSpecs.js (and SuperAdmin's copy)
— change them together. Hero banners have their own, stricter spec in
adminpanel/homepage.py (BANNER_IMAGE_SPEC), measured from the hero's CSS.

Why these numbers: customer cards draw every venue and event picture in one
3:2 frame (1600×1067 recommended — the size the apps already compress
uploads to on the device). An image far from 3:2 is NOT refused: the site
shows it whole on a blurred backdrop rather than cutting it, so shape is
only ever a warning. What IS refused is an image too small to stay sharp on
a card (under 400×300, either orientation) or absurdly large (over 8000px a
side). Type and the 5 MB size cap are checked by the views, as before.

An upload whose pixel size cannot be read from its header (the signature
check already proved it is a JPEG/PNG/WebP) is accepted without warnings —
refusing a valid photo over a parser gap would break vendor uploads.
"""
from adminpanel.image_info import image_size

CARD_RATIO = 3 / 2
MAX_SIDE = 8000

LISTING_IMAGE_SPEC = {
    'formats': ['JPEG', 'PNG', 'WebP'],
    'mimeTypes': ['image/jpeg', 'image/png', 'image/webp'],
    'frame': {'label': '3:2', 'value': CARD_RATIO},
    'recommended': {'width': 1600, 'height': 1067},
    'sharpFrom': {'width': 1200, 'height': 800},
    'minimum': {'width': 400, 'height': 300},
    # cover keeps less than this share of the image -> shown whole instead
    'minKeep': {'photo': 0.66, 'poster': 0.9},
}


def _keep(width, height):
    """Share of the image a 3:2 cover crop keeps (the smaller of the two)."""
    ratio = width / height
    if ratio > CARD_RATIO:
        return CARD_RATIO / ratio
    return ratio / CARD_RATIO


def listing_image_check(data, kind='photo'):
    """
    → (problem, info). `problem` is a refusal message or None; `info` is
    {'width', 'height', 'warnings'} (sizes None when unreadable).
    `kind` is 'photo' (venue photos, service images) or 'poster' (event
    photos — words on them are never cut, so a looser shape is "shown
    whole" sooner).
    """
    size = image_size(data)
    if not size:
        return None, {'width': None, 'height': None, 'warnings': []}
    width, height = size
    info = {'width': width, 'height': height, 'warnings': []}
    spec = LISTING_IMAGE_SPEC
    if width > MAX_SIDE or height > MAX_SIDE:
        return (f'Images larger than {MAX_SIDE}px on a side are not accepted — resize it first.', info)
    long_side, short_side = max(width, height), min(width, height)
    minimum, recommended = spec['minimum'], spec['recommended']
    if long_side < minimum['width'] or short_side < minimum['height']:
        return (
            f'This image is {width}×{height}px — too small to stay sharp on a card. '
            f'Use at least {minimum["width"]}×{minimum["height"]}px '
            f'(recommended {recommended["width"]}×{recommended["height"]}px).',
            info,
        )
    keep = _keep(width, height)
    if keep < spec['minKeep'].get(kind, spec['minKeep']['photo']):
        info['warnings'].append(
            f'{width}×{height} is far from 3:2, so cards show it whole with blurred edges.'
        )
    elif keep < 0.85:
        info['warnings'].append(
            f'{width}×{height} isn’t 3:2, so cards trim its edges (they keep the middle '
            f'{round(keep * 100)}%).'
        )
    sharp = spec['sharpFrom']
    if long_side < sharp['width'] or short_side < sharp['height']:
        info['warnings'].append(
            f'{width}×{height} may look soft on large screens; '
            f'{sharp["width"]}×{sharp["height"]} or more stays sharp.'
        )
    return None, info
