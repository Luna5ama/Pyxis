# Pyxis

Personal Codex skills.

[`pyxis-subagent-mcp`](pyxis-subagent-mcp/README.md) provides a Rust stdio MCP bridge for Claude Code, Codex CLI, and DeepSeek Harness specialists.

Available skills:

- [`pyxis-image-compare`](pyxis-image-compare/SKILL.md) — **Pyxis image compare** creates offline HTML image comparisons with lossless AVIF for PNG/lossless WebP and configurable lossy AVIF for JPEG/lossy WebP.
- [`optimization-loop`](optimization-loop/SKILL.md) runs measured optimization rounds; visual correctness checks can use Pyxis image compare.
- [`pyxis-loop`](pyxis-loop/SKILL.md) tracks persistent implementation tasks and their evidence.
- [`pyxis-subagent`](pyxis-subagent/SKILL.md) describes the Pyxis native-agent MCP and image-comparison handoffs.

Install a skill by copying its directory into `$CODEX_HOME/skills` (normally `~/.codex/skills`), excluding test/cache files. When upgrading from `image-compare`, replace that installed directory with `pyxis-image-compare` so only the new name is discoverable. See the image comparison skill for its Pillow and libavif dependencies and encoder cache location.

Each skill should live in its own directory and use `SKILL.md` as its entrypoint:

```text
skill-name/
├── SKILL.md
├── scripts/      # optional
├── references/   # optional
└── assets/       # optional
```
