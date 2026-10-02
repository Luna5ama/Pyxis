#!/usr/bin/env python3
"""Generate a self-contained, multi-dimensional image comparator."""

import argparse
import base64
import html
import json
from pathlib import Path
import sys


PAGE = r'''<!doctype html>
<html lang="zh-CN">
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
canvas{position:absolute;inset:0;display:block;cursor:grab;image-rendering:pixelated}
canvas.dragging{cursor:grabbing}
#message{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);margin:0;padding:12px;background:#202123e8;pointer-events:none;text-align:center}
footer{padding:8px 16px;color:#b8babf;font-size:12px;line-height:1.8;border-top:1px solid #494a4d}
#status{color:#ededed}#scale-error{color:#ffab9d}
</style>
</head>
<body>
<header>
<div id="dimensions"></div>
<div id="toolbar">
<label>对比维度 <select id="compare" aria-label="对比维度"></select></label>
<button id="previous" title="左方向键">← 上一项</button>
<button id="next" title="右方向键">下一项 →</button>
<button id="toggle" title="空格">当前 ↔ 上一个</button>
<label><input id="blink" type="checkbox" disabled>自动闪烁（1 秒）</label>
<label>缩放 <select id="preset" aria-label="缩放预设"></select></label>
<label><input id="scale" aria-label="缩放倍率" type="number" step="any" value="1">×</label>
<span id="scale-error" role="alert"></span>
</div>
</header>
<main id="viewport"><canvas id="canvas" aria-label="图片对比画布"></canvas><p id="message" role="status">正在加载图片…</p></main>
<footer><div id="status"></div><div>左右箭头：沿所选维度切换 · 空格：当前／上一个组合 · 拖动：平移 · Alt＋滚轮：缩放 · nearest-neighbor</div></footer>
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
let current = [...data.images[0].values], previous = null;
let scale = 1, centerX = 0, centerY = 0, initialized = false;
let shownImage = null, timer = null, generation = 0, drag = null;
const cache = new Map();

function load(item) {
  if (!cache.has(item.src)) {
    const entry = {image: null, error: null};
    entry.promise = new Promise(resolve => {
      const image = new Image();
      image.onload = () => {entry.image = image; resolve(entry)};
      image.onerror = () => {entry.error = '图像加载失败：' + item.label; resolve(entry)};
      image.src = item.src;
    });
    cache.set(item.src, entry);
  }
  return cache.get(item.src);
}

function draw() {
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
  message.textContent = item ? '正在加载图片…' : '此组合无图片';
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
preset.add(new Option('自定义', 'custom')); preset.value = '1';
function setScale(value) {
  const error = document.querySelector('#scale-error');
  if (!Number.isFinite(value) || value <= 0) {
    error.textContent = '请输入有限的正数'; scaleInput.value = String(scale); return;
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

canvas.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  canvas.setPointerCapture(event.pointerId);
  drag = {id: event.pointerId, x: event.clientX, y: event.clientY};
  canvas.classList.add('dragging');
});
canvas.addEventListener('pointermove', event => {
  if (!drag || drag.id !== event.pointerId) return;
  centerX -= (event.clientX - drag.x) / scale;
  centerY -= (event.clientY - drag.y) / scale;
  drag.x = event.clientX; drag.y = event.clientY; draw();
});
function endDrag(event) {
  if (!drag || drag.id !== event.pointerId) return;
  drag = null; canvas.classList.remove('dragging');
  if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
}
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);
canvas.addEventListener('lostpointercapture', endDrag);
new ResizeObserver(draw).observe(viewport);
window.addEventListener('resize', draw);
render();
</script>
</body>
</html>
'''


def read_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("清单必须是 JSON 对象")
    title = manifest.get("title", "图片对比")
    if not isinstance(title, str):
        raise ValueError("title 必须是字符串")
    entries = manifest.get("images")
    if not isinstance(entries, list) or not entries:
        raise ValueError("images 必须是非空数组")
    names = None
    dimensions = []
    images = []
    seen = set()
    encoded = {}
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"图片 {index} 必须是对象")
        values = entry.get("dimensions")
        if not isinstance(values, dict) or not values:
            raise ValueError(f"图片 {index} 的 dimensions 必须是非空对象")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise ValueError(f"图片 {index} 的维度名称和取值必须是字符串")
        if names is None:
            names = list(values)
            dimensions = [{"name": name, "values": []} for name in names]
        if set(values) != set(names):
            raise ValueError(f"图片 {index} 的维度集合不一致")
        combination = tuple(values[name] for name in names)
        if combination in seen:
            raise ValueError(f"图片 {index} 重复完整组合：{combination}")
        seen.add(combination)
        for dimension, value in zip(dimensions, combination):
            if value not in dimension["values"]:
                dimension["values"].append(value)
        source = entry.get("path")
        if not isinstance(source, str) or not source:
            raise ValueError(f"图片 {index} 的 path 必须是非空字符串")
        image_path = (path.parent / source).resolve()
        if image_path not in encoded:
            raw = image_path.read_bytes()
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif raw.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise ValueError(f"不支持的图片格式：{image_path}（仅 PNG、JPEG、WebP）")
            encoded[image_path] = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        images.append({"values": combination, "src": encoded[image_path], "label": source})
    return title, {"dimensions": dimensions, "images": images}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON 图片清单")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出 HTML 路径")
    args = parser.parse_args()
    try:
        title, data = read_manifest(args.manifest.resolve())
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
