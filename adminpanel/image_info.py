"""
Pixel dimensions of an uploaded JPEG, PNG or WebP — read from the file's own
header bytes, so banner uploads can be checked without adding Pillow.

Only the three formats the upload endpoints accept (venues.storage
ALLOWED_TYPES) are understood. Anything unreadable returns None and the caller
refuses the file: a banner whose size we cannot verify is not one we can
promise will crop correctly.
"""
import struct

# JPEG start-of-frame markers that carry the image size. C4 (DHT), C8 (JPG)
# and CC (DAC) share the 0xC_ range but are NOT frames.
_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def _png_size(data):
    # Signature (8) + IHDR length (4) + 'IHDR' (4) + width (4) + height (4).
    if len(data) < 24 or data[12:16] != b'IHDR':
        return None
    width, height = struct.unpack('>II', data[16:24])
    return width, height


def _jpeg_size(data):
    index = 2                                   # past the FFD8 start marker
    length = len(data)
    while index + 4 <= length:
        if data[index] != 0xFF:
            return None                         # lost sync — not a JPEG we trust
        marker = data[index + 1]
        if marker == 0xFF:                      # fill byte before a marker
            index += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2                          # stand-alone markers, no length
            continue
        if marker == 0xD9 or marker == 0xDA:    # end of image / start of scan
            return None                         # no frame header before the data
        segment = struct.unpack('>H', data[index + 2:index + 4])[0]
        if segment < 2:
            return None
        if marker in _SOF_MARKERS:
            if index + 9 > length:
                return None
            height, width = struct.unpack('>HH', data[index + 5:index + 9])
            return width, height
        index += 2 + segment
    return None


def _webp_size(data):
    if len(data) < 30 or data[0:4] != b'RIFF' or data[8:12] != b'WEBP':
        return None
    chunk = data[12:16]
    if chunk == b'VP8X':
        # 24-bit little-endian canvas width-1 / height-1.
        width = 1 + int.from_bytes(data[24:27], 'little')
        height = 1 + int.from_bytes(data[27:30], 'little')
        return width, height
    if chunk == b'VP8 ':
        # Lossy: frame tag (3) + start code 9D 01 2A, then 14-bit sizes.
        if data[23:26] != b'\x9d\x01\x2a':
            return None
        width = struct.unpack('<H', data[26:28])[0] & 0x3FFF
        height = struct.unpack('<H', data[28:30])[0] & 0x3FFF
        return width, height
    if chunk == b'VP8L':
        # Lossless: signature 0x2F then 14-bit width-1 and height-1 packed.
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], 'little')
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_size(data):
    """(width, height) in pixels, or None when the header can't be read."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return None
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            size = _png_size(data)
        elif data[:2] == b'\xff\xd8':
            size = _jpeg_size(data)
        elif data[:4] == b'RIFF':
            size = _webp_size(data)
        else:
            size = None
    except (struct.error, IndexError):
        size = None
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    return size
