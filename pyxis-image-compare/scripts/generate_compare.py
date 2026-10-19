#!/usr/bin/env python3
"""Generate a self-contained, multi-dimensional image comparator."""

import argparse
import base64
import html
import json
import io
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
import sys


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{display:flex;flex-direction:column;background:#202123;color:#ededed;font:14px system-ui,sans-serif}
header{padding:6px 10px;background:#2b2c2f;border-bottom:1px solid #494a4d;display:flex;flex-direction:column;gap:5px}
label{display:flex;align-items:center;gap:4px;white-space:nowrap}
select,button,input[type=number]{font:inherit;color:inherit;background:#303236;border:1px solid #606268;border-radius:4px;padding:3px 7px}
button{cursor:pointer}button:hover{background:#414349}button:disabled{opacity:.45;cursor:default}
button[aria-pressed=true]{background:#34694e;border-color:#71ad88}
input[type=number]{width:80px}input{accent-color:#71ad88}
#dimensions{display:flex;flex-direction:column;gap:4px}
.dimension-row{display:flex;align-items:baseline;gap:7px}
.dimension-name{flex:0 0 85px;font-size:12px;color:#b8babf;overflow-wrap:anywhere}
.dimension-options{display:flex;flex:1;min-width:0;gap:4px;flex-wrap:wrap}
.dimension-options button{max-width:100%;overflow-wrap:anywhere;text-align:left}
#toolbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding-top:4px;border-top:1px solid #494a4d}
#viewport{position:relative;flex:1;min-height:80px;overflow:hidden;touch-action:none}
#viewport{cursor:grab}
#viewport.dragging{cursor:grabbing}
canvas{position:absolute;inset:0;display:block;image-rendering:pixelated}
#copy-image{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:inherit}
#copy-image[hidden]{display:none}
#message{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);margin:0;padding:12px;background:#202123e8;pointer-events:none;text-align:center}
footer{padding:8px 16px;color:#b8babf;font-size:12px;line-height:1.8;border-top:1px solid #494a4d}
#status{color:#ededed}#scale-error{color:#ffab9d}
</style>
</head>
<body>
<header>
<div id="dimensions"></div>
<div id="toolbar">
<label>Comparison dimension <select id="compare" aria-label="Comparison dimension"></select></label>
<button id="previous" title="Left arrow key">← Previous</button>
<button id="next" title="Right arrow key">Next →</button>
<button id="toggle" title="Space">Current ↔ Previous</button>
<label><input id="blink" type="checkbox" disabled>Auto blink (1 second)</label>
<label>Zoom <select id="preset" aria-label="Zoom preset"></select></label>
<label><input id="scale" aria-label="Zoom factor" type="number" step="any" value="1">×</label>
<label title="When disabled, copy the visible image with zoom and cropping, excluding surrounding blank space"><input id="copy-original" type="checkbox" checked>Copy original image</label>
<span id="scale-error" role="alert"></span>
</div>
</header>
<main id="viewport"><canvas id="canvas" aria-label="Image comparison canvas"></canvas><img id="copy-image" alt="" draggable="false" hidden><p id="message" role="status">Loading image…</p></main>
<footer><div id="status"></div><div>Left/right arrows: cycle through the selected dimension · Space: current/previous combination · Drag: pan · Alt + wheel: zoom · Nearest-neighbor scaling (no interpolation)</div></footer>
<script id="data" type="application/json">__DATA__</script>
<script>
'use strict';
const data = JSON.parse(document.querySelector('#data').textContent);
const names = data.dimensions.map(d => d.name);
const imageKey = values => JSON.stringify(values);
const images = new Map(data.images.map(item => [imageKey(item.values), item]));
const controls = [];
const compare = document.querySelector('#compare');
const canvas = document.querySelector('#canvas');
const viewport = document.querySelector('#viewport');
const ctx = canvas.getContext('2d');
const message = document.querySelector('#message');
const status = document.querySelector('#status');
const blink = document.querySelector('#blink');
const toggle = document.querySelector('#toggle');
const preset = document.querySelector('#preset');
const scaleInput = document.querySelector('#scale');
const copyOriginal = document.querySelector('#copy-original');
const copyImage = document.querySelector('#copy-image');
let current = [...data.images[0].values], previous = null;
let scale = 1, centerX = 0, centerY = 0, initialized = false;
let shownImage = null, timer = null, generation = 0, drag = null;
let preparedCopy = false;
const cache = new Map();

function load(item) {
  if (!cache.has(item.src)) {
    const entry = {image: null, error: null};
    entry.promise = new Promise(resolve => {
      const image = new Image();
      image.onload = () => {entry.image = image; resolve(entry)};
      image.onerror = () => {entry.error = 'Failed to load image: ' + item.label; resolve(entry)};
      image.src = item.src;
    });
    cache.set(item.src, entry);
  }
  return cache.get(item.src);
}

function draw() {
  preparedCopy = false;
  // Native "Copy image" targets this real image, not the viewport-sized canvas.
  copyImage.hidden = !shownImage;
  if (shownImage && copyImage.getAttribute('src') !== shownImage.src) copyImage.src = shownImage.src;
  if (!shownImage) copyImage.removeAttribute('src');
  const width = viewport.clientWidth, height = viewport.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.max(1, Math.round(width * ratio));
  const pixelHeight = Math.max(1, Math.round(height * ratio));
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  canvas.style.width = width + 'px'; canvas.style.height = height + 'px';
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.imageSmoothingEnabled = false;
  if (!shownImage || !width || !height) return;
  // Clip in image coordinates: allocate only the viewport, even at high zoom.
  const left = Math.max(0, centerX - width / (2 * scale));
  const top = Math.max(0, centerY - height / (2 * scale));
  const right = Math.min(shownImage.naturalWidth, centerX + width / (2 * scale));
  const bottom = Math.min(shownImage.naturalHeight, centerY + height / (2 * scale));
  if (right <= left || bottom <= top) return;
  // Use whole source pixels so clipping does not change the sampling grid.
  const sx = Math.floor(left), sy = Math.floor(top);
  const sw = Math.ceil(right) - sx, sh = Math.ceil(bottom) - sy;
  ctx.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);
  ctx.drawImage(shownImage, sx, sy, sw, sh,
    width / 2 + (sx - centerX) * scale,
    height / 2 + (sy - centerY) * scale, sw * scale, sh * scale);
}

function prepareCopy() {
  if (!shownImage) return false;
  if (preparedCopy) return true;
  if (copyOriginal.checked) {
    if (copyImage.getAttribute('src') !== shownImage.src) copyImage.src = shownImage.src;
    preparedCopy = true;
    return true;
  }
  const width = viewport.clientWidth, height = viewport.clientHeight;
  const imageX = width / 2 - centerX * scale;
  const imageY = height / 2 - centerY * scale;
  const left = Math.max(0, imageX), top = Math.max(0, imageY);
  const right = Math.min(width, imageX + shownImage.naturalWidth * scale);
  const bottom = Math.min(height, imageY + shownImage.naturalHeight * scale);
  if (right <= left || bottom <= top) return false;
  const output = document.createElement('canvas');
  // Export in CSS pixels: display density must not add an extra zoom factor.
  output.width = Math.max(1, Math.round(right - left));
  output.height = Math.max(1, Math.round(bottom - top));
  const outputCtx = output.getContext('2d');
  outputCtx.imageSmoothingEnabled = false;
  const sx = Math.max(0, Math.floor((left - imageX) / scale));
  const sy = Math.max(0, Math.floor((top - imageY) / scale));
  const sw = Math.min(shownImage.naturalWidth, Math.ceil((right - imageX) / scale)) - sx;
  const sh = Math.min(shownImage.naturalHeight, Math.ceil((bottom - imageY) / scale)) - sy;
  // Match the display sampling grid, including fractional pan offsets.
  outputCtx.scale(output.width / (right - left), output.height / (bottom - top));
  outputCtx.drawImage(shownImage, sx, sy, sw, sh,
    imageX + sx * scale - left, imageY + sy * scale - top, sw * scale, sh * scale);
  copyImage.src = output.toDataURL('image/png');
  preparedCopy = true;
  return true;
}
copyOriginal.addEventListener('change', draw);
copyImage.addEventListener('contextmenu', event => {
  stopBlink();
  if (!prepareCopy()) event.preventDefault();
});

function updateStatus() {
  const size = shownImage ? ' · ' + shownImage.naturalWidth + '×' + shownImage.naturalHeight : '';
  status.textContent = names.map((name, i) => name + ': ' + current[i]).join(' / ') + size + ' · ' + scale + '×';
}

function preload() {
  const axis = Number(compare.value);
  for (const value of data.dimensions[axis].values) {
    const values = [...current]; values[axis] = value;
    const item = images.get(imageKey(values));
    if (item) load(item);
  }
}

function render() {
  const token = ++generation;
  controls.forEach((buttons, i) => {
    buttons.forEach(button => button.setAttribute('aria-pressed', button.dataset.value === current[i]));
  });
  toggle.disabled = blink.disabled = previous === null;
  shownImage = null;
  const item = images.get(imageKey(current));
  message.hidden = false;
  message.textContent = item ? 'Loading image…' : 'No image for this combination';
  const display = entry => {
    if (token !== generation) return;
    shownImage = entry.image;
    if (shownImage && !initialized) {
      centerX = shownImage.naturalWidth / 2; centerY = shownImage.naturalHeight / 2;
      initialized = true;
    }
    message.hidden = Boolean(shownImage);
    if (entry.error) message.textContent = entry.error;
    updateStatus(); draw();
  };
  if (item) {
    const entry = load(item);
    if (entry.image || entry.error) display(entry);
    else entry.promise.then(display);
  }
  updateStatus(); draw(); preload();
}

function stopBlink() {clearInterval(timer); timer = null; blink.checked = false}
function select(values) {
  stopBlink();
  if (imageKey(values) === imageKey(current)) return;
  previous = current; current = values; render();
}
function swap() {
  if (!previous) return;
  [current, previous] = [previous, current]; render();
}
function step(delta) {
  stopBlink();
  const axis = Number(compare.value), values = data.dimensions[axis].values;
  const next = [...current];
  next[axis] = values[(values.indexOf(current[axis]) + delta + values.length) % values.length];
  select(next);
}

data.dimensions.forEach((dimension, i) => {
  const row = document.createElement('div'); row.className = 'dimension-row';
  const label = document.createElement('span'); label.className = 'dimension-name';
  label.textContent = dimension.name;
  const options = document.createElement('div'); options.className = 'dimension-options';
  options.setAttribute('role', 'group'); options.setAttribute('aria-label', dimension.name);
  const buttons = dimension.values.map(value => {
    const button = document.createElement('button');
    button.textContent = value; button.dataset.value = value;
    button.addEventListener('click', () => {
      const next = [...current]; next[i] = value; select(next);
    });
    options.append(button); return button;
  });
  controls.push(buttons); row.append(label, options);
  document.querySelector('#dimensions').append(row);
  compare.add(new Option(dimension.name, String(i)));
});
compare.addEventListener('change', preload);
document.querySelector('#previous').addEventListener('click', () => step(-1));
document.querySelector('#next').addEventListener('click', () => step(1));
toggle.addEventListener('click', () => {stopBlink(); swap()});
blink.addEventListener('change', () => {
  clearInterval(timer);
  timer = blink.checked && previous ? setInterval(swap, 1000) : null;
});
document.addEventListener('keydown', event => {
  if (event.target.closest('select,input,textarea,[contenteditable="true"]') || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key === 'ArrowLeft') {event.preventDefault(); step(-1)}
  if (event.key === 'ArrowRight') {event.preventDefault(); step(1)}
  if (event.code === 'Space') {event.preventDefault(); stopBlink(); swap()}
});

const scales = [0.25, 0.5, 1, 2, 4, 8, 16];
scales.forEach(value => preset.add(new Option(value + '×', String(value))));
preset.add(new Option('Custom', 'custom')); preset.value = '1';
function setScale(value) {
  const error = document.querySelector('#scale-error');
  if (!Number.isFinite(value) || value <= 0) {
    error.textContent = 'Enter a finite positive number'; scaleInput.value = String(scale); return;
  }
  error.textContent = ''; scale = value; scaleInput.value = String(value);
  preset.value = scales.includes(value) ? String(value) : 'custom';
  updateStatus(); draw();
}
preset.addEventListener('change', () => {
  if (preset.value !== 'custom') setScale(Number(preset.value));
  else scaleInput.focus();
});
scaleInput.addEventListener('change', () => setScale(scaleInput.valueAsNumber));
viewport.addEventListener('wheel', event => {
  if (!event.altKey || event.deltaY === 0) return;
  event.preventDefault();
  const next = scale * (event.deltaY < 0 ? 2 : 0.5);
  if (Number.isFinite(next) && next > 0) setScale(next);
}, {passive: false});

viewport.addEventListener('pointerdown', event => {
  // Prepare before the native context menu resolves its image resource.
  if (event.button === 2) {stopBlink(); prepareCopy(); return}
  if (event.button !== 0) return;
  viewport.setPointerCapture(event.pointerId);
  drag = {id: event.pointerId, x: event.clientX, y: event.clientY};
  viewport.classList.add('dragging');
});
viewport.addEventListener('pointermove', event => {
  if (!drag || drag.id !== event.pointerId) return;
  centerX -= (event.clientX - drag.x) / scale;
  centerY -= (event.clientY - drag.y) / scale;
  drag.x = event.clientX; drag.y = event.clientY; draw();
});
function endDrag(event) {
  if (!drag || drag.id !== event.pointerId) return;
  drag = null; viewport.classList.remove('dragging');
  if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
}
viewport.addEventListener('pointerup', endDrag);
viewport.addEventListener('pointercancel', endDrag);
viewport.addEventListener('lostpointercapture', endDrag);
new ResizeObserver(draw).observe(viewport);
window.addEventListener('resize', draw);
render();
</script>
</body>
</html>
'''


def source_encoding(raw):
    """Inspect the actual container/codec, never infer WebP mode from its suffix."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(raw) < 33 or raw[12:16] != b"IHDR":
            raise ValueError("Invalid PNG header")
        if raw[24] > 8:
            raise ValueError("Lossless AVIF conversion currently requires PNG depth <= 8; refusing to reduce bit depth")
        return "PNG", True
    if raw.startswith(b"\xff\xd8\xff"):
        return "JPEG", False
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        end = struct.unpack_from("<I", raw, 4)[0] + 8
        if end != len(raw):
            raise ValueError("Invalid WebP RIFF size")
        offset = 12
        payload_mode = None
        while offset + 8 <= end:
            tag = raw[offset:offset + 4]
            size = struct.unpack_from("<I", raw, offset + 4)[0]
            if offset + 8 + size > end:
                raise ValueError("Truncated WebP chunk")
            if tag in (b"ANIM", b"ANMF"):
                raise ValueError("Animated images are not supported")
            if tag in (b"VP8L", b"VP8 "):
                if payload_mode is not None:
                    raise ValueError("Multiple WebP image payloads")
                payload_mode = tag == b"VP8L"
            offset += 8 + size + (size & 1)
        if offset != end or payload_mode is None:
            raise ValueError("Invalid WebP image payload")
        return "WEBP", payload_mode
    raise ValueError("Unsupported image format (only PNG, JPEG, and WebP are supported)")


def encode_avif(image_path, encoder, lossy_quality=80):
    try:
        from PIL import Image, ImageOps, features
    except ImportError as error:
        raise ValueError("Install Pillow with AVIF support (Pillow >= 11.3) to generate comparisons") from error
    if not features.check("avif"):
        raise ValueError("Pillow must include AVIF decoding support for lossless verification")
    raw = image_path.read_bytes()
    source_format, lossless = source_encoding(raw)
    with Image.open(io.BytesIO(raw)) as original:
        if getattr(original, "n_frames", 1) != 1:
            raise ValueError(f"Animated images are not supported: {image_path}")
        original.load()
        reference = ImageOps.exif_transpose(original)
        pixels = reference.convert("RGBA" if reference.has_transparency_data else "RGB")
        with tempfile.TemporaryDirectory(prefix="image-compare-") as directory:
            directory = Path(directory)
            # avifenc accepts PNG/JPEG. Normalize WebP and grayscale to RGB(A) PNG
            # so identity-matrix lossless encoding uses all three color planes.
            # Normalize EXIF orientation into pixels when needed; retain ICC metadata.
            if source_format == "WEBP" or original.mode in ("1", "L", "LA") or original.getexif().get(274, 1) != 1:
                source = directory / "input.png"
                metadata = {key: reference.info[key] for key in ("icc_profile", "exif", "xmp") if key in reference.info}
                pixels.save(source, **metadata)
            else:
                source = directory / ("input.png" if source_format == "PNG" else "input.jpg")
                source.write_bytes(raw)
            output = directory / "output.avif"
            command = [encoder, "--codec", "aom", "--speed", "6", "--jobs", "2"]
            if lossless:
                command += ["--lossless", "--yuv", "444", "--range", "full", "--cicp", "1/13/0"]
            else:
                command += ["--qcolor", str(lossy_quality), "--qalpha", "100", "--yuv", "444"]
            command += [str(source), str(output)]
            options = {"creationflags": subprocess.IDLE_PRIORITY_CLASS} if os.name == "nt" else {}
            result = subprocess.run(command, capture_output=True, text=True, **options)
            if result.returncode:
                raise ValueError(f"AVIF encoding failed for {image_path}: {result.stderr.strip()} {result.stdout.strip()}")
            avif = output.read_bytes()
            if lossless:
                with Image.open(io.BytesIO(avif)) as decoded:
                    reference = reference.convert(pixels.mode)
                    decoded_pixels = decoded.convert(pixels.mode)
                    if decoded_pixels.size != reference.size or decoded_pixels.tobytes() != reference.tobytes():
                        raise ValueError(f"AVIF lossless pixel verification failed: {image_path}")
            return avif


def find_encoder(encoder=None):
    selected = encoder or os.environ.get("AVIFENC") or shutil.which("avifenc")
    if selected:
        return str(selected)
    cached = Path.home() / ".cache" / "pyxis" / "libavif" / ("avifenc.exe" if os.name == "nt" else "avifenc")
    if cached.is_file():
        return str(cached)
    raise ValueError("avifenc is required: install libavif tools or pass --avifenc PATH")


def read_manifest(path, encoder=None, lossy_quality=80):
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    title = manifest.get("title", "Image Comparison")
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    entries = manifest.get("images")
    if not isinstance(entries, list) or not entries:
        raise ValueError("images must be a nonempty array")
    names = None
    dimensions = []
    images = []
    seen = set()
    encoded = {}
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Image {index} must be an object")
        values = entry.get("dimensions")
        if not isinstance(values, dict) or not values:
            raise ValueError(f"Image {index}: dimensions must be a nonempty object")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise ValueError(f"Image {index}: dimension names and values must be strings")
        if names is None:
            names = list(values)
            dimensions = [{"name": name, "values": []} for name in names]
        if set(values) != set(names):
            raise ValueError(f"Image {index}: dimension set does not match")
        combination = tuple(values[name] for name in names)
        if combination in seen:
            raise ValueError(f"Image {index}: duplicate complete combination: {combination}")
        seen.add(combination)
        for dimension, value in zip(dimensions, combination):
            if value not in dimension["values"]:
                dimension["values"].append(value)
        source = entry.get("path")
        if not isinstance(source, str) or not source:
            raise ValueError(f"Image {index}: path must be a nonempty string")
        image_path = (path.parent / source).resolve()
        if image_path not in encoded:
            avif = encode_avif(image_path, find_encoder(encoder), lossy_quality)
            encoded[image_path] = "data:image/avif;base64," + base64.b64encode(avif).decode("ascii")
        images.append({"values": combination, "src": encoded[image_path], "label": source})
    return title, {"dimensions": dimensions, "images": images}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON image manifest")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output HTML path")
    parser.add_argument("--avifenc", help="Path to libavif avifenc (otherwise AVIFENC, PATH, or the Pyxis cache)")
    parser.add_argument("--lossy-quality", type=int, default=80, choices=range(1, 100), metavar="1..99",
                        help="AVIF quality for lossy input only (default: 80)")
    args = parser.parse_args()
    try:
        title, data = read_manifest(args.manifest.resolve(), args.avifenc, args.lossy_quality)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        head, _, tail = PAGE.partition("__DATA__")
        page = head.replace("__TITLE__", html.escape(title)) + payload + tail
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(page, encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
