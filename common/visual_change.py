"""Small image fingerprints shared by automatic visual observers."""
from PIL import Image


def image_fingerprint(image):
    def bits(region):
        pixels = list(region.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes())
        return tuple(pixels[y * 9 + x] > pixels[y * 9 + x + 1]
                     for y in range(8) for x in range(8))
    width, height = image.size
    return bits(image), bits(image.crop((width / 4, height / 4, width * 3 / 4, height * 3 / 4)))


def image_difference(previous, current):
    if previous is None:
        return 1.0
    return sum(weight * sum(a != b for a, b in zip(old, new)) / len(old)
               for weight, old, new in zip((0.6, 0.4), previous, current))
