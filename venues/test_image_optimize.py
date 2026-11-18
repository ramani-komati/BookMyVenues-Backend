"""Uploaded photos are shrunk when large; never enlarged, never broken."""
import io

from django.test import SimpleTestCase
from PIL import Image

from .image_optimize import MAX_SIDE, optimize_image


def jpeg(size, quality=100):
    buffer = io.BytesIO()
    # Noise-free but detailed enough that quality matters.
    image = Image.linear_gradient('L').resize(size).convert('RGB')
    image.save(buffer, 'JPEG', quality=quality)
    return buffer.getvalue()


class OptimizeImageTests(SimpleTestCase):
    def test_large_photo_is_downscaled_and_smaller(self):
        original = jpeg((4000, 3000))
        data, content_type = optimize_image(original, 'image/jpeg')
        self.assertEqual(content_type, 'image/jpeg')
        self.assertLess(len(data), len(original))
        with Image.open(io.BytesIO(data)) as out:
            self.assertEqual(max(out.size), MAX_SIDE)
            self.assertEqual(out.size, (2000, 1500))     # aspect ratio kept

    def test_small_or_unknown_files_are_left_alone(self):
        small = jpeg((300, 200), quality=60)
        self.assertLessEqual(len(optimize_image(small, 'image/jpeg')[0]), len(small))  # never bigger
        self.assertEqual(optimize_image(b'<svg/>', 'image/svg+xml'), (b'<svg/>', 'image/svg+xml'))
        self.assertEqual(optimize_image(b'not an image', 'image/jpeg'), (b'not an image', 'image/jpeg'))
