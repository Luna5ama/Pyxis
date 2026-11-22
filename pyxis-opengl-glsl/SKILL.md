---
name: pyxis-opengl-glsl
description: 用于编写、审查和优化 OpenGL GLSL 着色器，涵盖 SSBO 编译耗时、访存方式、寄存器生命周期和计算着色器工作组大小。
---

# Pyxis OpenGL GLSL

- Avoid reserved identifiers such as `packed`, `buffer`, and `sample`.
- Avoid excessive or very large fixed-size SSBO arrays: some drivers aggressively unroll loops over them, causing extreme compile times. Prefer a trailing unsized array where the layout permits it.
- Prefer aligned wide loads/stores when all components are needed: one `vec4` access over four scalar `float` accesses. Preserve buffer layout and verify actual code generation/timing.
- Prefer `texelFetch` over `imageLoad` for equivalent read-only access; `imageLoad` has been slower in observed workloads. Preserve format, coordinates, and memory visibility/barrier semantics; this is not a universal speed guarantee.
- Keep branch-specific state inside its branch; construct/load values near use and consume them promptly. Avoid carrying large structs or many `inout` values across branches or helper calls. Consider cheap recomputation instead of retaining long-lived intermediates.
- Minimize simultaneously live values: process independent samples or stages sequentially instead of preloading all inputs; keep only fields needed later, and pass only required values to helpers rather than copying entire state structs.
- Start Ampere compute tuning at 128 or 256 threads per workgroup. On CC 8.6, 64-thread groups hit the 16-block/SM limit at 32 of 48 possible resident warps; this occupancy ceiling does not apply identically to CC 8.0. Larger groups do not guarantee saturation: registers, shared memory, and workload still matter.
- Treat performance guidance as hypotheses: compare affected-pass GPU timings under matching conditions, and inspect registers/spills/active warps when needed. Accept measured wins, not source-level simplification alone.
