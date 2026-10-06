---
name: image-compare
description: Generate an offline, self-contained HTML image comparator from local PNG, JPEG, or WebP images, with arbitrary comparison dimensions, nearest-neighbor zoom, panning, and history toggling.
---

# Image Comparator

Use `scripts/generate_compare.py` to turn an image manifest into a single offline HTML file. The generator uses only the Python standard library and embeds the original images without transcoding or exposure adjustments.

The generated HTML uses an English interface, including default titles, control labels, buttons, messages, and usage help. When creating a manifest, use English for the title and natural-language dimension names and values. Preserve filenames, model names, version numbers, and other technical identifiers that require exact matching. The generator displays manifest content as supplied and does not automatically translate user-provided data.

```sh
python <skill-directory>/scripts/generate_compare.py input.json -o comparison.html
```

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
- "Copy original image" is enabled by default: the browser's right-click "Copy image" command copies the complete original image. When disabled, it copies the visible image region with nearest-neighbor zoom and cropping, excluding surrounding blank space. Output scale uses CSS pixels and is independent of display pixel density. Opening the context menu stops auto blink.

Create the manifest, run the script, and deliver the generated HTML. Verify that the requested conditions map to the correct images. The generator does not extract data or generate source images.
