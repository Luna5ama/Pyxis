# Pyxis

Personal Codex skills.

[`pyxis-subagent-mcp`](pyxis-subagent-mcp/README.md) provides a Rust stdio MCP bridge for Claude Code, Codex CLI, and DeepSeek Harness specialists.

[`pyxis-codex-app-mcp`](pyxis-codex-app-mcp/README.md) is a dependency-free stdio adapter that exposes the running Codex desktop app's tools to Claude Code and Claude Desktop.

Available skills:

- [`pyxis-image-compare`](pyxis-image-compare/SKILL.md) — **Pyxis image compare** creates offline HTML image comparisons with lossless AVIF for PNG/lossless WebP and configurable lossy AVIF for JPEG/lossy WebP.
- [`pyxis-opengl-glsl`](pyxis-opengl-glsl/SKILL.md) guides OpenGL shader authors on control flow, register lifetimes, texture and buffer access, and compute workgroup sizing. [Measured cases and counterexamples](docs/research/opengl-glsl.md)
- [`pyxis-optimization-loop`](pyxis-optimization-loop/SKILL.md) runs measured optimization rounds.
- [`pyxis-loop`](pyxis-loop/SKILL.md) tracks persistent implementation tasks and their evidence.
- [`pyxis-subagent`](pyxis-subagent-mcp/SKILL.md) describes the Pyxis native-agent MCP.

Design discussion is maintained as `vela-design` in [Endoqa/Vela](https://github.com/Endoqa/Vela). Replace the old installed `pyxis-design-discussion` directory with `vela-design`; no forwarding skill is retained.

Install a skill by copying its directory into `$CODEX_HOME/skills` (normally `~/.codex/skills`), excluding test/cache files. The `pyxis-subagent` skill shares `pyxis-subagent-mcp/` with its MCP implementation; install only its `SKILL.md` and `agents/` into `$CODEX_HOME/skills/pyxis-subagent/`, leaving sources, local configs, and build artifacts in the repository. When upgrading from `image-compare` or `opengl-glsl`, replace the old installed directory with `pyxis-image-compare` or `pyxis-opengl-glsl`, respectively, so each skill is discoverable under one name. See the image comparison skill for its Pillow and libavif dependencies and encoder cache location.

Each installed skill should live in its own directory and use `SKILL.md` as its entrypoint:

```text
skill-name/
├── SKILL.md
├── scripts/      # optional
├── references/   # optional
└── assets/       # optional
```
