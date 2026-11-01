# Pyxis

Personal Codex skills.

[`pyxis-subagent-mcp`](pyxis-subagent-mcp/README.md) provides a Rust stdio MCP bridge for Claude Code, Codex CLI, and DeepSeek Harness specialists.

Available skills:

- [`pyxis-image-compare`](pyxis-image-compare/SKILL.md) — **Pyxis image compare** creates offline HTML image comparisons with lossless AVIF for PNG/lossless WebP and configurable lossy AVIF for JPEG/lossy WebP.
- [`pyxis-optimization-loop`](pyxis-optimization-loop/SKILL.md) runs measured optimization rounds.
- [`pyxis-loop`](pyxis-loop/SKILL.md) tracks persistent implementation tasks and their evidence.
- [`pyxis-subagent`](pyxis-subagent-mcp/SKILL.md) describes the Pyxis native-agent MCP.

Design discussion is maintained as `vela-design` in [Endoqa/Vela](https://github.com/Endoqa/Vela). Replace the old installed `pyxis-design-discussion` directory with `vela-design`; no forwarding skill is retained.

Install a skill by copying its directory into `$CODEX_HOME/skills` (normally `~/.codex/skills`), excluding test/cache files. The `pyxis-subagent` skill shares `pyxis-subagent-mcp/` with its MCP implementation; install only its `SKILL.md` and `agents/` into `$CODEX_HOME/skills/pyxis-subagent/`, leaving sources, local configs, and build artifacts in the repository. When upgrading from `image-compare`, replace that installed directory with `pyxis-image-compare` so only the new name is discoverable. See the image comparison skill for its Pillow and libavif dependencies and encoder cache location.

Each installed skill should live in its own directory and use `SKILL.md` as its entrypoint:

```text
skill-name/
├── SKILL.md
├── scripts/      # optional
├── references/   # optional
└── assets/       # optional
```
