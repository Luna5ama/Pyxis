---
name: pyxis-image-compare
description: Generate an offline, self-contained HTML image comparator from local PNG, JPEG, or WebP images, with arbitrary comparison dimensions, nearest-neighbor zoom, panning, and history toggling.
---

# Pyxis image compare

Use `scripts/generate_compare.py` to turn an image manifest into a single offline HTML file. The generator embeds AVIF images without resizing or exposure adjustments. PNG and lossless WebP inputs use true lossless AVIF; JPEG and lossy WebP inputs use high-quality lossy AVIF (quality 80, 4:4:4, lossless alpha). WebP mode is determined from its VP8L/VP8 payload, including extended WebP containers, rather than its filename. Lossless output is decoded and checked pixel-for-pixel before embedding; quality 100 alone is not a substitute for lossless encoding.

Requirements: Python with Pillow >= 11.3 and AVIF decoding support, plus the official [libavif tools](https://github.com/AOMediaCodec/libavif/releases) (`avifenc` with the AOM encoder). Locate an existing encoder first; if absent, obtain the official platform binary in a local tool cache and pass `--avifenc PATH`. The script does not download dependencies or fall back to lossy compression for lossless sources. Use `AVIFENC` or PATH for an already installed encoder. The generator also discovers `~/.cache/pyxis/libavif/avifenc` (`avifenc.exe` on Windows), allowing a cached official binary without changing PATH. Run time-insensitive encoding at the lowest process priority; on Windows the generator launches the encoder at Idle priority.

Encoding preserves image dimensions, transparency, ICC profiles and EXIF orientation. Animated PNG/WebP and PNG samples above 8 bits are rejected explicitly rather than silently flattened or reduced in precision. This is lossless relative to decoded input pixels; a PNG made from a JPEG still follows the PNG lossless policy. Lossy recompression adds some distortion and is unsuitable for exact pixel-difference analysis.

The generated HTML uses an English interface, including default titles, control labels, buttons, messages, and usage help. When creating a manifest, use English for the title and natural-language dimension names and values. Preserve filenames, model names, version numbers, and other technical identifiers that require exact matching. The generator displays manifest content as supplied and does not automatically translate user-provided data.

```sh
python <skill-directory>/scripts/generate_compare.py input.json -o comparison.html --avifenc /path/to/avifenc
```

`--lossy-quality 1..99` adjusts only lossy-source encoding; default 80 favors comparison fidelity while reducing size. Never reduce quality automatically for lossless input, even if lossless AVIF is larger than the source. Original source files remain untouched.

Example manifest (image paths are resolved relative to the manifest; absolute paths are also supported):

```json
{
  "title": "Image Comparison",
  "images": [
    {"path": "before.png", "dimensions": {"Scene": "Indoor", "Version": "Before"}},
    {"path": "after.png", "dimensions": {"Scene": "Indoor", "Version": "After"}}
  ]
}
```

All dimensions are equal; a simple A/B comparison needs only a "Version" dimension. Every entry must use the same nonempty set of dimensions, with string names and values. Dimensions and values are ordered by first occurrence. Missing combinations are allowed; duplicate complete combinations, empty manifests, and missing files produce errors.

Page controls:

- Each dimension has its own row of option buttons that wrap as needed. Dimensions can be combined freely; missing combinations show "No image for this combination". Comparison and zoom controls share a separate row.
- Choose a "Comparison dimension", then use the left/right arrow keys or Previous/Next to cycle through its values.
- Space toggles between the current and previous complete combinations. Auto blink alternates every second and stops when a combination is changed manually.
- Zoom defaults to 1×, with presets of 0.25, 0.5, 1, 2, 4, 8, and 16×. Any finite positive decimal is accepted. Scaling uses nearest-neighbor sampling without interpolation.
- Drag with the primary mouse button to pan. Alt + wheel up doubles the zoom; Alt + wheel down halves it. Zoom preserves the viewport center, and changing any dimension preserves zoom and position. Focused input controls retain native keyboard shortcuts.
- "Copy original image" is enabled by default: the browser's right-click "Copy image" command copies the complete embedded image (AVIF-decoded pixels, including recompression for lossy sources). When disabled, it copies the visible image region with nearest-neighbor zoom and cropping, excluding surrounding blank space. Output scale uses CSS pixels and is independent of display pixel density. Opening the context menu stops auto blink.

Create the manifest, run the script, and deliver the generated HTML. Verify that the requested conditions map to the correct images. The generator does not extract data or generate source images.
