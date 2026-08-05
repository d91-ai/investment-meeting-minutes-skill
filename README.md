# 投资会议纪要整理 Skill

本仓库保存 Codex 使用的中文投资会议纪要整理 skill 包，用于把投资研究场景中的会议录音、转写稿、DOCX/TXT/Markdown 文稿或音频加文字混合材料，整理成可人工复核、可本地归档的 Markdown 会议纪要。

当前定位是 **main orchestrator + MAS process automation + deterministic validation**。会议纪要任务默认进入 MAS 编排，由主流程根据长录音、噪音重、多人边界不清、多标的混杂、音频/文档冲突、高风险事实或遗漏风险，增量选择 specialist subagents。Subagents 只返回结构化审计 artifact；最终 Markdown 和本地归档产物只由主流程统一生成。Verification sidecar 只作为内部审计文件，不作为正式交付物。

## 推荐调用方式

在 Codex 中附上源文件后，可直接使用以下提示词：

```text
请使用 investment-meeting-minutes Skill，整理「输入文件路径」。这是文稿输入，按「多人复盘会／公司交流／专家交流」处理。保留原始发言顺序、第一人称、观点强度、条件和时间信息；修正明显转写错误，核验公司名称和股票代码，无法确认的内容列入“存疑与待确认”。严格执行完整审核链，所有审核结果必须落实到最终文档；审核未完成或存在流程阻塞时不得导出，应直接说明未完成环节。最终输出为「完整输出路径及文件名.md」。
```

会议类型必须明确为“多人复盘会”“公司交流”或“专家交流”。替换输入文件、会议类型、输出文件名和保存目录即可复用；音频与文稿属于同一场会议时，应在同一任务中同时提供。

## 核心原则

- 基础 skill 是唯一会议纪要 skill；会议类型只影响最终格式细节。
- 主 workflow 固定为：转录 -> 校对 -> 识别 -> 联网核验 -> 编辑 -> 排版 -> 验证。
- 主流程是最终纪要的唯一写作者，避免多流程拼接造成格式、口径和风格割裂。
- MAS 是高风险任务的过程自动化层，不是多个 agent 拼接终稿的写作机制。
- Specialist agents 只产出与当前风险直接对应的结构化 artifact，例如 `transcript_audit`、`source_reconciliation`、`entity_verification_report`、`target_attribution_review`、`fidelity_review` 和 `export_manifest`。
- `发言整理` 默认是可复核原文纪要，不输出摘要、压缩稿或研报化改写。
- 音频加文稿输入时，先完成音频转录，再比较音频转录、`aligned_transcript` 和文稿质量；以覆盖更完整、发言顺序更可靠、逐字性更强、噪声和遗漏更少的一侧作为正文主源。
- 联网核验只确认实体、代码、术语和公开事实，不补写会议没有说过的内容。
- 外部核验只发送候选实体、代码、术语和必要公开事实关键词，不发送原始会议长段、发言人身份、私有链接、未公开客户/订单上下文或机密 source text。
- Validator 检查格式、章节、表头、编码、样例回归，以及可选 verification 审计 artifact 的结构完整性；它不伪验证联网核验是否真实发生，也不做看好/看空等语义硬判定。

## 目录结构

- `skills/投资会议纪要整理/SKILL.md`：主 workflow、输入边界、转录、校对、核验、编辑和导出规则。
- `skills/投资会议纪要整理/references/output_contract.md`：最终 Markdown 格式契约和存疑表规则。
- `skills/投资会议纪要整理/references/verification_policy.md`：公司名、代码、术语、目标归因和存疑项核验规则。
- `skills/投资会议纪要整理/references/archive_naming_contract.md`：原始输入归档和最终文件命名规则。
- `skills/投资会议纪要整理/references/runtime_readiness_guide.md`：本地 ASR、文稿处理和导出 readiness。
- `skills/投资会议纪要整理/references/meeting_types/`：多人复盘会、公司交流、专家交流的正文格式依据。
- `skills/投资会议纪要整理/references/mas_orchestration_contract.md`：MAS 编排边界、artifact schema、自动/人工决策规则。
- `skills/投资会议纪要整理/references/regression_samples/`：合成回归样例和负例。
- `skills/投资会议纪要整理/scripts/`：归档、转录、校对、查询、MAS 编排、验证和导出脚本。

纪要脱敏和 RAG 入库准备不再内置在本仓库；需要脱敏时使用独立仓库 `d91-ai/minute-sanitization-skill`。

## 运行档位

- `fast_document`：短、干净、发言人清楚、低存疑的纯文稿。文稿 -> 主流程写草稿 -> 非人名业务实体和高风险事实联网核验 -> 脚本格式验证 -> 导出。
- `standard`：普通文稿或音频加文稿。音频加文稿场景先完成音频转录，再对比音频转录、`aligned_transcript` 和文稿质量，选择质量更高的一侧写正文，另一侧用于交叉对比。
- `strict_audio`：音频-only、长录音、噪音重、音频/文档冲突或高风险事实密集。执行对应 readiness profile，再进入人工关口、主流程审查和本地导出。

需要 MAS 额外过程自动化的情况包括：长录音、噪音重、多人边界不清、音频与文档冲突、多标的混杂、名称/代码/数字/客户/供应商/术语存疑、最终成稿前需要补漏。短、干净、低风险的 `fast_document` 不默认派发风险专项 subagents。

## 仓库级检查

合并、规则变更或脚本变更后优先运行：

```bash
python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py README.md AGENTS.md skills/投资会议纪要整理/SKILL.md --require-cjk --portable-skill
python3 skills/投资会议纪要整理/scripts/run_meeting_minutes_regression.py --json
python3 skills/投资会议纪要整理/scripts/validate_mas_artifacts.py skills/投资会议纪要整理/references/regression_samples/mas_artifacts_valid.json --require-artifact source_reconciliation --json
python3 skills/投资会议纪要整理/scripts/summarize_mas_decisions.py skills/投资会议纪要整理/references/regression_samples/mas_artifacts_valid.json --require-artifact source_manifest --require-artifact source_reconciliation --require-artifact entity_verification_report --require-artifact doubtful_items --require-artifact fidelity_review --require-artifact export_manifest --json
MAS_TMP="$(mktemp -d /tmp/mas-dry-run.XXXXXX)"
python3 skills/投资会议纪要整理/scripts/run_mas_dry_run.py --request-json skills/投资会议纪要整理/references/regression_samples/mas_task_request_audio_plus_document.json --artifact-fixture skills/投资会议纪要整理/references/regression_samples/mas_artifacts_valid.json --task-dir "$MAS_TMP" --out "$MAS_TMP/mas_dry_run_trace.json" --json
```

具体纪要产物验证需要把 `NOTE.md` 和 sidecar 路径替换为真实文件；这些不是仓库级绿色回归：

```bash
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --require-term "我没有减仓" --forbid-term "发言人认为" --json
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --verification NOTE.verification.json --require-verification --json
```

本机运行环境 readiness 会受模型、LibreOffice、目录权限和 `INVESTMENT_MINUTES_WORKSPACE` 影响；失败表示环境未就绪，不等同于仓库回归失败：

```bash
export INVESTMENT_MINUTES_WORKSPACE="$HOME/Documents/会议纪要整理"
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile asr --strict
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile document
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile export
```

## MAS 操作说明

MAS 手动 walkthrough 使用唯一临时目录，不作为一次性全绿 validators 清单。Collector 输出必须以顶层 `ok: true` 作为继续门禁；当 `ok: false` 时，只读取 `next_action` 做补齐或修复，不消费 combined artifacts 作为有效结果。

```bash
MAS_DISPATCH="$(mktemp -d /tmp/mas-dispatch.XXXXXX)"
python3 skills/投资会议纪要整理/scripts/run_mas_phase_operator.py --request-json skills/投资会议纪要整理/references/regression_samples/mas_task_request_audio_plus_document.json --task-dir "$MAS_DISPATCH" --through-phase pre_draft --auto-source-manifest --json
```

首轮 operator 会生成绑定当前 `run_id`/`task_id` 的 prompt 和派发清单，并停在待收集 specialist 返回的状态。不要把仓库内固定 fixture 直接当成这次 run 的返回值；subagent 必须按本次 prompt 的 identity 返回，再用 `run_mas_phase_operator.py --task-dir "$MAS_DISPATCH" --return-json RETURN.json --through-phase pre_draft --json` 继续。

- `create_mas_source_manifest.py` 生成主流程自有 `source_manifest` artifact。
- `ingest_mas_artifact.py` 接收一个 subagent 返回 JSON，校验后写入 task-dir，并保留 invalid/duplicate 返回到 `repair_history/`。
- `collect_mas_artifacts.py` 发布 `mas_run_summary.json`、combined artifacts、next-action plan 和 operator state。
- `plan_mas_next_action.py` 把 collector 的 `next_action` 转成可执行清单。
- `record_mas_main_actions.py` 绑定 draft review 后的主流程修订动作、Markdown SHA-256 和 source-artifact digest。
- `run_mas_phase_operator.py` 串联 dispatch 初始化、返回 ingest、collector 和 next-action plan；它不启动 subagent、不写最终 Markdown。
- `run_mas_dry_run.py` 用合成 artifact 生成阶段化 MAS 执行轨迹；真实会议内容仍只能由主流程写入最终 Markdown。

## 输出边界

- 正式交付为最终 Markdown 会议纪要。
- 最终文件命名按会议类型分别使用 `YYYY-MM-DD - 会议系列.md`、`YYYY-MM-DD - 公司名 - 上市公司交流.md` 或 `YYYY-MM-DD - 主题 - 专家交流.md`。
- 无法确定会议系列、公司名或主题时，必须请用户确认，不导出占位文件名。
- Verification sidecar 只用于内部审计，不作为正式交付物。
- 最终交付不生成 Word 或 PDF。
- PDF 输入只作为附件归档；正文整理应使用音频、DOCX、TXT、Markdown 或用户另行提供的可读文本。

## 隐私边界

不要提交：

- 真实会议材料、原始录音、正式纪要或私有转写。
- 外部搜索或专业数据工具查询中，不发送原始会议长段、发言人身份、私有链接、未公开客户/订单上下文或机密 source text。
- 私有绝对路径、临时审阅链接、草稿链接、token、浏览器会话数据、API key 或认证 header。
- ASR 模型权重、下载缓存、虚拟环境或本机私有配置。

公共 fixtures 必须是合成或充分脱敏内容。

## 开发与发布约束

- 不直接修改 `main`；所有变更通过功能分支和 PR 合并。
- 不推送、开 PR、合并、强推、改写历史、删除分支、reset 或 stash，除非用户明确授权。
- 不同步、覆盖或删除 active local Codex install `~/.codex/skills`，除非用户明确授权。
- 不引入 LangGraph、CrewAI、AutoGen 等重型 Agent 框架。
- 改业务规则时同步更新对应 reference、回归样例或验证说明。
- 改输出格式时运行 Markdown validator 和相关回归检查。
