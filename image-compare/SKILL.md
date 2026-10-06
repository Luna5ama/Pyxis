---
name: image-compare
description: Generate an offline, self-contained HTML image comparator from local PNG, JPEG, or WebP images, with arbitrary comparison dimensions, nearest-neighbor zoom, panning, and history toggling.
---

# 图片对比器

用 `scripts/generate_compare.py` 将图片清单生成单个离线 HTML。仅依赖 Python 标准库，原图内嵌，不转码、不调整曝光。

```sh
python <skill目录>/scripts/generate_compare.py input.json -o comparison.html
```

清单示例（图片路径相对清单解析，也支持绝对路径）：

```json
{
  "title": "图片对比",
  "images": [
    {"path": "before.png", "dimensions": {"场景": "室内", "版本": "Before"}},
    {"path": "after.png", "dimensions": {"场景": "室内", "版本": "After"}}
  ]
}
```

所有维度平等；简单 A/B 只需一个“版本”维度。各条目必须使用相同的非空维度集合，名称和取值均为字符串。维度和取值按首次出现顺序排列。缺图组合允许存在；重复完整组合、空清单或文件缺失会报错。

网页操作：

- 每个维度独立一行选项按钮，放不下时自动换行；各维度可任意组合，无图组合显示“此组合无图片”。对比和缩放控件集中在另一行。
- 选择“对比维度”，用左右箭头或上一项／下一项沿该维度循环切换。
- 空格在当前和上一个完整组合间往返；自动闪烁每 1 秒往返，手动切换条件时停止。
- 默认 1×；预设 0.25、0.5、1、2、4、8、16×，也可输入任意有限正小数。采用 nearest-neighbor，无插值。
- 鼠标主键拖动平移，Alt＋滚轮向上放大 2 倍、向下缩小一半，缩放保持视口中心；切换任何条件都保留倍率及位置。输入控件聚焦时保留原生快捷键。
- “复制原图”默认开启：右键图片使用浏览器的“复制图片”时复制完整原图。关闭后复制当前可见图片区域，保留 nearest-neighbor 缩放和裁切，去掉外围空白；输出倍率按 CSS 像素计算，不受屏幕像素密度影响。打开右键菜单时停止自动闪烁。

为用户生成清单并运行脚本，交付生成的 HTML；验证所需条件和图片对应正确。生成器不负责提取数据或生成源图片。
