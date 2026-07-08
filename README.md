# 投资会议纪要整理 Skill

本仓库保存 Codex 使用的中文投资会议纪要整理 skill 包，用于把投资研究场景中的会议录音、转写稿、纪要草稿或音频加文字材料，整理成可人工复核、可本地归档的 Markdown 和 Word 会议纪要。

当前 `main` 分支定位是 **single main workflow + deterministic validation**：默认使用单一主流程完成归档、转录、校对、识别、联网核验、编辑、排版、验证和导出。高风险材料仍由主流程做额外自检，不再依赖 repo-scoped subagent 机制。

## 适用场景

- 音频会议：本地 ASR 转录后整理为正式纪要。
- 文稿会议：从已有转写稿、聊天记录或纪要草稿生成规范纪要。
- 音频加文稿：同时使用录音和文字材料，保留冲突和存疑。
- 公司名、代码、客户、供应商、术语、数字或时间存在不确定时，生成可复核的存疑表和可选 verification sidecar。
- 已整理纪要需要脱敏或 RAG 入库时，使用独立仓库 `d91-ai/minute-sanitization-skill`。

## 输入与输出

常见输入：

- 音频文件、视频转音频、已有转写稿、会议草稿、辅助参考文本。
- 会议日期、会议标题、会议类型、会议系列。
- 可选的 `timestamp_index.json`、verification JSON/JSONL、人工复核后的文本。

主要输出：

- 最终 Markdown 会议纪要。
- 同 stem 的 Word 文档。
- 原始输入归档副本。
- 仅在真实存疑存在时输出的 `## 二、存疑与待确认`。
- 可选 verification sidecar，用于审计非人名业务存疑项的核验过程。

最终纪要必须包含 `## 一、发言整理`。无真实存疑时，不输出“暂无存疑”。

## 核心原则

- 终稿只由主流程生成，避免多流程拼接造成格式、口径和风格不一致。
- `发言整理` 是可复核的按说话人整理稿，不是摘要、压缩稿或研报化改写。
- 保留发言顺序、人称、判断边界、数字、时间点、仓位动作和条件表达。
- 只删除纯口水词、明显 ASR 噪声、无意义重复和重复起手式。
- 公司名、股票代码、客户、供应商、数字、日期和专业术语必须先结合会议上下文核对；无法确认的内容进入存疑表。
- 外部来源只能用于核验名称、代码、术语和公开事实，不能补写会议中没有出现的新内容。

## 目录结构

- `skills/投资会议纪要整理/SKILL.md`：主 workflow、输入边界、转录、校对、核验、编辑和导出规则。
- `skills/投资会议纪要整理/references/output_contract.md`：最终 Markdown 和 Word 格式契约。
- `skills/投资会议纪要整理/references/verification_policy.md`：公司名、代码、术语、目标归因和存疑项核验规则。
- `skills/投资会议纪要整理/references/archive_naming_contract.md`：原始输入归档和最终文件命名规则。
- `skills/投资会议纪要整理/references/runtime_readiness_guide.md`：本地 ASR、文稿处理和导出 readiness。
- `skills/投资会议纪要整理/references/meeting_types/`：多人复盘会、公司交流、专家交流的正文格式依据。
- `skills/投资会议纪要整理/references/regression_samples/`：合成回归样例和负例。
- `skills/投资会议纪要整理/scripts/`：归档、转录、校对、查询、验证和导出脚本。
- 纪要脱敏和 RAG 入库准备 skill 已迁移到独立仓库 `d91-ai/minute-sanitization-skill`。

## 处理流程

1. 归档输入
   使用 `archive_raw_inputs.py` 复制原始材料，避免直接覆盖用户文件。

2. 转录
   音频输入默认使用本地 SenseVoiceSmall 作为主 ASR。Paraformer-Large 只作为辅助校对证据，用于公司名、代码、数字、英文缩写和专业词交叉检查；不得自动替换 SenseVoice 主转写。禁止降级使用 Whisper。

3. 校对
   使用 `process_transcript.py` 辅助清理明显 ASR 噪声、口水词和无意义重复，同时保留原说话人的视角、顺序和判断强度。

4. 识别与核验
   使用 `query_symbol_candidates.py` 做本地证券代码候选查询。业务实体和高风险事实确认前，应结合 `a-stock-data`、公告、交易所、官网、行业资料或其他可靠来源核验。

5. 编辑纪要
   按 `output_contract.md` 和对应会议类型 reference 输出元信息、`## 一、发言整理` 和必要的存疑表。会议类型默认为 `多人复盘会`；单家公司专场用 `公司交流`；专家问答用 `专家交流`。

6. 验证与导出
   用 validator 检查 Markdown、verification sidecar、timestamp index 和 Word 结构，再用 `export_to_obsidian.py` 导出 Markdown 与 Word。最终交付不生成 PDF。

## 常用命令

归档原始输入：

```bash
python3 skills/投资会议纪要整理/scripts/archive_raw_inputs.py \
  --date 2026-07-03 \
  --title "会议标题" \
  path/to/audio.m4a path/to/notes.txt
```

检查本地 ASR 模型缓存：

```bash
python3 skills/投资会议纪要整理/scripts/transcribe_audio.py --check-model-cache
```

转录音频：

```bash
python3 skills/投资会议纪要整理/scripts/transcribe_audio.py \
  path/to/audio.m4a \
  --output-dir work/transcripts \
  --output-format all
```

预处理转写稿：

```bash
python3 skills/投资会议纪要整理/scripts/process_transcript.py \
  work/transcripts/audio.txt \
  --output work/transcripts/audio.cleaned.txt
```

批量查询证券候选：

```bash
python3 skills/投资会议纪要整理/scripts/query_symbol_candidates.py \
  --batch-file terms.txt \
  --json
```

校验 Markdown：

```bash
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py \
  NOTE.md \
  --source-mode document \
  --json
```

校验音频纪要、时间戳和 verification sidecar：

```bash
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py \
  NOTE.md \
  --source-mode audio \
  --timestamp-mode reliable \
  --require-audio-timestamps \
  --timestamp-index timestamp_index.json \
  --require-reliable-timestamp-index \
  --verification NOTE.verification.json \
  --require-verification \
  --json
```

校验 Markdown 和 Word：

```bash
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py \
  NOTE.md \
  --word NOTE.docx \
  --json
```

导出 Markdown 和 Word：

```bash
python3 skills/投资会议纪要整理/scripts/export_to_obsidian.py \
  NOTE.md \
  --export-dir "$HOME/Documents/会议纪要整理/01 Projects/会议纪要" \
  --meeting-date 2026-07-03
```

运行回归样例：

```bash
python3 skills/投资会议纪要整理/scripts/run_meeting_minutes_regression.py --json
```

运行健康检查：

```bash
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile asr --strict
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile document
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile export
```

## 验证范围

`validate_meeting_minutes_contract.py` 会检查：

- Markdown 是否包含会议元信息和 `## 一、发言整理`。
- 会议类型是否符合对应 reference 的正文结构。
- 存疑表是否只在真实存疑存在时出现。
- 音频来源、可靠时间戳来源和文稿来源的存疑表列是否正确。
- `人工确认` 列是否保留为空。
- `--require-term` 指定的关键原文锚点是否仍在正文中。
- `--forbid-term` 指定的改写锚点是否未出现。
- verification sidecar 是否包含必要字段并与非人名存疑项一致。
- `timestamp_index.json` 是否包含可用于存疑定位的 sentence/phrase anchor 或短 VAD segment。
- 传入 `--word` 时，Word 文档结构、元信息表、存疑表和存疑词样式是否符合契约。

validator 只做结构、样例、sidecar、时间戳和 Word 样式检查；它不会伪验证联网核验是否真实发生。

## 本地环境提示

- Python 脚本文本读写必须显式使用 UTF-8；中文文件使用 UTF-8 without BOM 和 LF。
- Word 导出依赖 `python-docx`。
- ASR 首次使用前应先检查模型缓存，避免整理会议时临时下载模型。
- `check_investment_workflow_health.py --prepare-local-dirs` 只在首次部署时显式创建本地目录；日常检查默认不创建目录。
- 本仓库和本机 active skill 安装目录是两个面：仓库更新后，如需 Codex 立即使用新规则，还要同步到 `~/.codex/skills` 并开启新线程或重启 Codex。

## 隐私边界

不要提交：

- 真实会议材料、原始录音、正式纪要或私有转写。
- 私有绝对路径、临时审阅链接、草稿链接、token、浏览器会话数据、API key 或认证 header。
- ASR 模型权重、下载缓存、虚拟环境或本机私有配置。

公共 fixtures 必须是合成或充分脱敏内容。

## 开发与发布约束

- 不直接修改 `main`；所有变更通过功能分支和 PR 合并。
- 不引入 LangGraph、CrewAI、AutoGen 等重型 Agent 框架。
- 改业务规则时同步更新对应 reference、回归样例或验证说明。
- 改输出格式时运行 Markdown 和 Word validators。
- fixtures 必须是合成或充分脱敏内容。
