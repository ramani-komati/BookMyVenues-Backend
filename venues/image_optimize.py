"""
Shrink large uploaded photos before they are stored (QA audit 2026-10-30:
multi-megabyte venue photos were served as-is to phones).

  * longest side capped at MAX_SIDE px (2000 — sharp on any screen the site
    is shown on), aspect ratio kept;
  * the camera's EXIF orientation is applied, then EXIF (incl. GPS) is
    dropped;
  * same format and content type as uploaded (so stored paths and
    extensions stay right); JPEG/WebP re-encoded at QUALITY;
  * the optimised copy is used ONLY if it is smaller. Animated images, SVG,
    unknown types and anything Pillow cannot read are stored untouched, so an
    upload never fails because of this step.
"""
import io
import logging

logger = logging.getLogger(__name__)

MAX_SIDE = 2000
QUALITY = 82
FORMATS = {'image/jpeg': 'JPEG', 'image/jpg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP'}


def optimize_image(content, content_type):
    """(bytes, content_type) — the smaller of the original and an optimised copy."""
    fmt = FORMATS.get(str(content_type or '').lower())
    if not fmt or not content:
        return content, content_type
    try:
        from PIL import Image, ImageOps
    except ImportError:   # Pillow not installed — store as uploaded
        return content, content_type
    try:
        with Image.open(io.BytesIO(content)) as image:
            if getattr(image, 'is_animated', False):
                return content, content_type
            image = ImageOps.exif_transpose(image)
            if max(image.size) > MAX_SIDE:
                image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            if fmt == 'JPEG' and image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            out = io.BytesIO()
            options = {'optimize': True}
            if fmt in ('JPEG', 'WEBP'):
                options['quality'] = QUALITY
            if fmt == 'JPEG':
                options['progressive'] = True
            image.save(out, fmt, **options)   # no exif= → metadata dropped
    except Exception:  # noqa: BLE001 — never fail an upload over optimisation
        logger.warning('Image optimisation skipped (unreadable %s).', content_type)
        return content, content_type
    optimized = out.getvalue()
    return (optimized, content_type) if len(optimized) < len(content) else (content, content_type)
