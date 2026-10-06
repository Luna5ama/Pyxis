"""Browser regression checks; requires Pillow, Playwright, and installed Chrome."""

import base64
import ctypes
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright


GENERATOR = Path(__file__).resolve().parents[1] / "scripts/generate_compare.py"
spec = importlib.util.spec_from_file_location("generate_compare", GENERATOR)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class CopyImageTests(unittest.TestCase):
    def test_native_image_target(self):
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
            root = Path(directory)
            original = Image.new("RGB", (120, 80))
            original.putdata([(x * 2, y * 3, (x + y) % 256)
                              for y in range(80) for x in range(120)])
            original.save(root / "original.png")
            Image.new("RGB", (60, 40), "red").save(root / "other.png")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": [
                {"path": "original.png", "dimensions": {"version": "A", "scene": "1"}},
                {"path": "other.png", "dimensions": {"version": "B", "scene": "2"}},
            ]}), encoding="utf-8")
            title, data = generator.read_manifest(manifest)
            page_path = root / "compare.html"
            page_path.write_text(generator.PAGE.replace("__TITLE__", title).replace(
                "__DATA__", json.dumps(data)), encoding="utf-8")
            browser = playwright.chromium.launch(channel="chrome")
            try:
                for ratio in (1, 2):
                    with self.subTest(device_pixel_ratio=ratio):
                        context = browser.new_context(viewport={"width": 800, "height": 600},
                                                      device_scale_factor=ratio)
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(page_path.as_uri())
                        page.wait_for_function("shownImage !== null")
                        self.assertTrue(page.locator("#copy-original").is_checked())

                        def copied():
                            box = page.locator("#viewport").bounding_box()
                            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                            self.assertEqual(page.evaluate(
                                "([x,y]) => document.elementFromPoint(x,y).id", [x, y]), "copy-image")
                            page.mouse.click(x, y, button="right")
                            src = page.locator("#copy-image").get_attribute("src")
                            raw = base64.b64decode(src.split(",", 1)[1])
                            return raw, Image.open(io.BytesIO(raw)).convert("RGBA")

                        page.evaluate("setScale(16); centerX = 20; centerY = 30; draw()")
                        self.assertEqual(copied()[0], (root / "original.png").read_bytes())
                        page.locator("#copy-original").uncheck()
                        for scale in (0.25, 0.5, 1, 2):
                            page.evaluate("s => {centerX=60; centerY=40; setScale(s)}", scale)
                            _, image = copied()
                            self.assertEqual(image.size, (round(120 * scale), round(80 * scale)))
                            self.assertEqual(image.getchannel("A").getextrema(), (255, 255))
                            if scale >= 1:
                                self.assertEqual(image.tobytes(), original.convert("RGBA").resize(
                                    image.size, Image.Resampling.NEAREST).tobytes())

                        # Crop inside the image: the output is just the viewport.
                        page.evaluate("centerX=60; centerY=40; setScale(16)")
                        _, image = copied()
                        viewport_size = page.evaluate("[viewport.clientWidth, viewport.clientHeight]")
                        self.assertEqual(image.size, tuple(viewport_size))
                        self.assertEqual(image.getchannel("A").getextrema(), (255, 255))

                        # Partial overlap: discard the empty portion of the viewport.
                        page.evaluate("centerX=0; centerY=0; setScale(2)")
                        self.assertEqual(copied()[1].size, (240, 160))
                        page.evaluate("centerX=-30; centerY=-20; setScale(16)")
                        self.assertFalse(page.evaluate("prepareCopy()"))

                        # Drag still works through the native-image hit target.
                        page.evaluate("centerX=60; centerY=40; setScale(1)")
                        box = page.locator("#viewport").bounding_box()
                        page.mouse.move(box["x"] + 100, box["y"] + 100)
                        page.mouse.down()
                        page.mouse.move(box["x"] + 130, box["y"] + 120)
                        page.mouse.up()
                        self.assertEqual(page.evaluate("[centerX, centerY]"), [30, 20])
                        page.locator("#copy-original").check()
                        page.evaluate("select(['B', '2'])")
                        page.wait_for_function("shownImage && shownImage.naturalWidth === 60")
                        self.assertEqual(copied()[0], (root / "other.png").read_bytes())
                        page.evaluate("select(['A', '2'])")
                        self.assertTrue(page.locator("#copy-image").is_hidden())
                        self.assertIsNone(page.locator("#copy-image").get_attribute("src"))
                        self.assertEqual(errors, [])
                        context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    # Background verification should not compete with foreground applications.
    if hasattr(ctypes, "windll"):
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x40)
    unittest.main()
