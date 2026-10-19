"""Real AVIF round-trip and manifest checks; requires Pillow and libavif avifenc."""

import base64
import ctypes
import importlib.util
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms, ImageOps

GENERATOR = Path(__file__).resolve().parents[1] / "scripts/generate_compare.py"
spec = importlib.util.spec_from_file_location("generate_compare", GENERATOR)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class AvifTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoder = generator.find_encoder()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = Image.frombytes("RGBA", (33, 25), random.Random(42).randbytes(33 * 25 * 4))
        self.icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    def roundtrip(self, path, **options):
        raw = generator.encode_avif(path, self.encoder, **options)
        self.assertIn(b"avif", raw[:32])
        decoded = Image.open(io.BytesIO(raw))
        decoded.load()
        self.addCleanup(decoded.close)
        return decoded

    def test_lossless_pixels_and_profiles(self):
        for filename, options in [("image.png", {}), ("image.webp", {"lossless": True, "exact": True})]:
            with self.subTest(filename=filename):
                path = self.root / filename
                self.image.save(path, icc_profile=self.icc, **options)
                decoded = self.roundtrip(path)
                self.assertEqual(decoded.convert("RGBA").tobytes(), self.image.tobytes())
                self.assertEqual(decoded.info.get("icc_profile"), self.icc)
        hidden = Image.new("RGBA", (16, 16), (255, 12, 42, 0))
        path = self.root / "hidden.png"
        hidden.save(path)
        self.assertEqual(self.roundtrip(path).convert("RGBA").tobytes(), hidden.tobytes())

    def test_grayscale_palette_and_orientation(self):
        palette = Image.new("P", (17, 19))
        palette.putpalette([i for v in range(256) for i in (v, 255-v, v//2)])
        palette.putdata([i % 256 for i in range(17*19)])
        palette.info["transparency"] = 0
        for image in [Image.new("L", (17, 19), 143), Image.new("LA", (17, 19), (143, 57)), palette]:
            with self.subTest(mode=image.mode):
                path = self.root / "converted.png"
                image.save(path)
                self.assertEqual(self.roundtrip(path).convert("RGBA").tobytes(), image.convert("RGBA").tobytes())
        for suffix in ("png", "webp", "jpg"):
            with self.subTest(orientation=suffix):
                path = self.root / ("oriented." + suffix)
                exif = Image.Exif()
                exif[274] = 6
                self.image.convert("RGB").save(path, exif=exif, **({"lossless": True} if suffix == "webp" else {}))
                decoded = self.roundtrip(path)
                self.assertEqual(decoded.size, (25, 33))
                if suffix != "jpg":
                    with Image.open(path) as source:
                        self.assertEqual(decoded.convert("RGB").tobytes(), ImageOps.exif_transpose(source).convert("RGB").tobytes())

    def test_lossy_formats_keep_alpha_and_use_content_detection(self):
        for filename in ("lossy.webp", "photo.jpg"):
            with self.subTest(filename=filename):
                path = self.root / filename
                source = self.image if filename.endswith("webp") else self.image.convert("RGB")
                source.save(path, quality=85, icc_profile=self.icc)
                self.assertFalse(generator.source_encoding(path.read_bytes())[1])
                decoded = self.roundtrip(path)
                with Image.open(path) as original:
                    self.assertEqual(decoded.size, original.size)
                    self.assertEqual(decoded.info.get("icc_profile"), self.icc)
                    self.assertNotEqual(decoded.convert("RGB").tobytes(), original.convert("RGB").tobytes())
                    if filename.endswith("webp"):
                        self.assertEqual(decoded.convert("RGBA").getchannel("A").tobytes(), original.getchannel("A").tobytes())
        path = self.root / "misleading.jpg"
        self.image.save(path, format="WEBP", lossless=True, exact=True, icc_profile=self.icc)
        self.assertEqual(generator.source_encoding(path.read_bytes()), ("WEBP", True))
        self.assertEqual(self.roundtrip(path).convert("RGBA").tobytes(), self.image.tobytes())

    def test_reject_unsupported_precision_and_animation(self):
        path = self.root / "deep.png"
        Image.new("I;16", (16, 16), 40000).save(path)
        with self.assertRaisesRegex(ValueError, "bit depth"):
            self.roundtrip(path)
        for suffix in ("png", "webp"):
            path = self.root / ("animated." + suffix)
            self.image.save(path, save_all=True, append_images=[Image.new("RGBA", self.image.size, "blue")], duration=100)
            with self.assertRaisesRegex(ValueError, "Animated"):
                self.roundtrip(path)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            generator.source_encoding(b"not an image")

    def test_manifest_embeds_avif_and_encodes_shared_source_once(self):
        self.image.save(self.root / "source.png")
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"images": [
            {"path": "source.png", "dimensions": {"Version": "Before"}},
            {"path": "source.png", "dimensions": {"Version": "After"}},
        ]}), encoding="utf-8")
        with patch.object(generator, "encode_avif", wraps=generator.encode_avif) as encode:
            _, data = generator.read_manifest(path, self.encoder)
            self.assertEqual(encode.call_count, 1)
        for entry in data["images"]:
            self.assertTrue(entry["src"].startswith("data:image/avif;base64,"))
            with Image.open(io.BytesIO(base64.b64decode(entry["src"].split(",", 1)[1]))) as decoded:
                self.assertEqual(decoded.convert("RGBA").tobytes(), self.image.tobytes())


if __name__ == "__main__":
    if hasattr(ctypes, "windll"):
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x40)
    unittest.main()
