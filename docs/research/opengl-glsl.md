# 从 Alpha-Piscium 的 pass 优化提炼 GLSL 写法

截至 2026-11-22，以下案例取自 Alpha-Piscium 的源码差异、实机对照和后续纠正。提交号可在该仓库用 `git show` 查看，`.vibris/` 路径相对原测量检出。安装包只带[技能入口](../../pyxis-opengl-glsl/SKILL.md)和可移植的[代码示例](../../pyxis-opengl-glsl/references/patterns.md)。

## 用 spirv-opt 指导原生 GLSL

10 月 20 日的目标是在 `2.0/dev` 改 GI 追踪、时域复用和阴影采样的手写源码。第一轮七个候选共 46 组 720p 计时。GI 延后构造样本、阴影 helper 少传字段、时域改用 `out` 等都没有可确认的收益。时域链溢出的重读方案初看快约 1.8%，同源码却有约 0.847 和 0.870 ms 两档，配对区间跨零。复查入口是 `.vibris/spirv-source-opt-20261020/resume-result.json`。

随后在时域复用展开后的模块里，重复出现了 87 条 `OpIsNan` 和 87 条 `OpIsInf`。追到源码，是共享 reservoir 有限值检查被带入许多调用点。两个修订的差别决定了可教给下一位作者的写法。

| 修订 | 静态线索 | 目标 GPU 上发生了什么 |
| --- | --- | --- |
| `0e6b3c63c`，四个谓词改为 `FLT_MAX` 范围判断 | 同一时域模块条件分支 311 → 183 | 最初 576 项谓词比较一致。后续让原版和候选各自编译并经过真实的取反、量化和累加路径，发现候选会接受某些 NaN。 |
| `30be7f0c1`，FP32 指数位判断，保留浮点正值比较，并整理调用方的范围检查 | 只替换四个谓词的对照为 311 → 223，`OpPhi` 为 394 → 306 | 以原始 `isnan/isinf` 版为参考，四组各 16,384 个 GPU 用例分别回读，结果全部一致。最终源码还保留了求和溢出、半精度量化和 Jacobian 的必要保护。 |

RTX 3080 Ti、720p 的时域复用 pass，Non-cube p50 从 0.844800 降到 0.672768 ms，森林从 0.865280 降到 0.670208 ms。原始版与最终版来自两次共享同一中间对照的正反序测量，最终正有限值特化在同一作业内单独测得 6.68%–6.77% 降幅。整组改动与单个谓词各有自己的比较范围。原件见 `.vibris/temporal-spirv-guide-20261020/result.json` 和 `.vibris/gi-range-checks-20261020/result.json`，后者同目录的 `spirv/` 保留了二进制模块。

本次从三份留存的优化后 `.spv` 重新反汇编，核对了两版候选和基线的静态计数。GPU 特殊值的比较依据是作业 `34ca1ecd-7538-4f05-9baf-2418f3c25618` 的分组回读记录与匹配哈希，两份原始 `.bin` 在回执记载的位置已不可读。运行计时来自原生 GLSL，当时完整回写 dev 的尝试遇到了声明映射歧义。

## 从回写 GLSL 看见的生命周期

| pass 与源码 | 可迁移的写法 | 观测边界 |
| --- | --- | --- |
| 天空合成，`e57e3374a` | 天空分支才构造散射结果，地面分支继续重置历史。 | 恢复 SSA rewrite 后，Non-cube 的 `composite13` p50 为 317.696 → 291.840 µs。 |
| DirectLighting，`4f5f62c2a` | 先求日月辐照度再解码材质，表面 BRDF 传实际字段，次表面散射独立计算，精确零辐照度可以跳过着色。 | 优化 ZIP 对优化 ZIP 的组合改动为 342.016 → 196.608 µs。另一次对当时 dev 直编源码的比较约为 133 → 197 µs，两个基线必须分开。 |
| GI 重投影，`2f18b2610` | 让 gather 及权重先归约为 `float`，再写其他历史。该工具链中 `void` helper 加 `out float` 保住了归约位置。 | 更早的源码候选曾把 `gi_reproject` 函数内的 Phi 降至零，仍从 198.656 升到 235.520 µs。应检查回写表达式及实际 pass，`out` 和 Phi 数本身都不能预测收益。 |
| VoxelTree，`7bb8f24c1` | 六个固定面共用同一实现，以常量面号调用，让轴索引和掩码能被折叠。 | 优化 ZIP 的 720p p50 为 240.640 → 238.336 µs。小固定集合值得比较，大循环另看代码体积与编译成本。 |

## 在其他 pass 上核对边界

- **沿着调用方需要的结果缩短追踪。** `92610e9be` 在体素节点读取前算好单比特掩码，Non-cube 的初始追踪 p50 仅减少 3.072 µs。空间复用只查有限线段可见性，`e83ed4ed1` 改成首次遮挡即可返回，结合空节点跳过等改动，三个 720p 场景的可见性 pass 降低 10.3%–20.0%。最近命中查询仍保留自己的语义。
- **分支由它能省掉的工作定价。** 云层 Worley 噪声的两个峰值仅需保留数值，`b187f5cbb` 改用 `min/max` 后目标 pass 从 1.428736 降到 1.235968 ms。GI 主模糊有另一种分布，`c46abf3ff` 给零权重样本跳过颜色读取，Non-cube 从 0.578560 降到 0.522240 ms，后模糊仍无条件读取。
- **取样由消费者需要的分量决定。** 时域置信度对邻域同一分量做 gather，`58429d46d` 在 Non-cube 的目标 pass 降低 2.65%。重投影需要每点完整 RGBA，`c0a5e3a23` 改成整 texel 读取后降低 1.34%。只读链表改走 sampler 时，`054f7c35b` 单独保留了当次会重写的图块的 image 访问。gather 的基础 mip、四点次序以及 image 写入后的纹理读取可见性由 OpenGL 定义。[GLSL 4.60 规范](https://registry.khronos.org/OpenGL/specs/gl/GLSLangSpec.4.60.pdf) · [OpenGL 4.6 规范](https://registry.khronos.org/OpenGL/specs/gl/glspec46.core.pdf)
- **相邻 lane 的地址和每条 lane 的负担一起看。** 11 月 4 日，`048bd6060` 将各自跨步扫描改成协作读取相邻向量并合并占用掩码，三个 720p 场景的 `prepare2` p50 降低 10.0%–10.8%。11 月 22 日的空八分体筛选却让树读取减少约 14.2%，初始追踪从 2.011 升到 2.438 ms，记录在 `2ad7a7dc0` 的 `docs/experiments/2026-11-22-gi-preparation.md`。工作组的驻留上限也要按具体 GPU 区分。[NVIDIA Ampere Tuning Guide](https://docs.nvidia.com/cuda/ampere-tuning-guide/index.html#occupancy)

入口沿用 Vela 的技能维护约定，只给能改变作者下一步的条件和动作。样例随技能安装，便于直接对照源码。性能结论沿用 Pyxis 的验收尺度，保留原始基线、反例和实际消费者。三个示例代码块已合在一个桌面 OpenGL compute shader 中编译、优化、回写 GLSL、再编译并通过 SPIR-V 校验。[Vela 维护准则](https://github.com/Endoqa/Vela/blob/main/AGENTS.md) · [Pyxis Optimization Loop](../../pyxis-optimization-loop/SKILL.md)
