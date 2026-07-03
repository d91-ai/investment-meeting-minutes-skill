# Repository Development Rules

- Do not modify `main` directly. Use a feature branch for every change.
- Do not push, open pull requests, merge, force-push, rewrite history, delete branches, reset, or stash unless the user explicitly authorizes it.
- Chinese text files must be UTF-8 without BOM and use LF line endings.
- Python text I/O for Markdown, TXT, JSON, CSV, TSV, YAML, logs, SRT, and VTT must pass `encoding="utf-8"` explicitly.
- Do not commit real meeting materials, raw recordings, private transcripts, production minutes, private temporary URLs, tokens, browser session data, API keys, model weights, or local private configuration.
- Fixtures must be synthetic or sufficiently desensitized.
- Do not commit private absolute paths, `file://` URLs, temporary workflow URLs, `Authorization` headers, Bearer tokens, browser session data, API keys, or private local endpoints in reusable artifacts.
- Do not introduce LangGraph, CrewAI, AutoGen, or other heavy Agent frameworks for this skill package.
- When changing business rules, update the relevant regression or eval fixture in the same change.
- When changing output format rules, run the Markdown and Word validators.
- Final Markdown may be created or modified only by the main workflow under the base skill and selected meeting-type reference. 最终纪要只能由主流程生成或修改。
