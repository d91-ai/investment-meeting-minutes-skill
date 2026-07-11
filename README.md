# 投资会议纪要整理 Skill

这个仓库保存 Codex 使用的中文投资会议纪要整理 skill 包，用于把投资研究场景中的会议录音、转写稿、纪要草稿或音频+文字混合材料，整理成可人工复核、可本地归档的 Markdown 会议纪要。

当前定位是 **single main workflow + deterministic validation**，并逐步升级为 **main orchestrator + MAS process automation**：默认使用最快的单主流程路径，在长录音、噪音重、多人边界不清、多标的混杂、音频/文档冲突或高风险事实较多时，由 MAS 专家层自动处理过程审查和结构化中间产物。最终 Markdown 和本地归档产物只由主流程统一生成；verification sidecar 仅作为内部审计文件，不作为正式交付物。

## 核心原则

- 主流程负责结构化中间检查，例如发言整理候选、标的/代码清单、存疑核验清单、音频/文档冲突摘要和遗漏检查。
- 主流程是最终会议纪要的唯一写作者，避免多流程直接拼接造成格式、口径和风格割裂。
- 基础 skill 是唯一会议纪要 skill；会议类型只影响最终格式细节。
- MAS 的目标是提高自动化效率、降低常规人工参与度、提升最终产物质量；它是高风险任务的过程自动化层，不是多个 agent 拼接终稿的写作机制。
- Specialist agents 只产出结构化 artifact，例如 `transcript_audit`、`source_reconciliation`、`entity_verification_report`、`target_attribution_review`、`fidelity_review` 和 `export_manifest`；主流程负责裁决、自动修正、标存疑、请求人工确认和最终交付。
- 主 workflow 固定为：转录 -> 校对 -> 识别 -> 联网核验 -> 编辑 -> 排版 -> 验证。
- `发言整理` 默认是可复核原文纪要，不输出摘要、压缩稿或研报化改写；多人复盘会每段标的标题只覆盖该段明确看好的证券标的，不把顺带提及、负向、客户/供应商、竞争对手、上下游或背景对象写入标的行。
- 音频+文稿输入时，先完成音频转录，再比较音频转录、`aligned_transcript` 和文稿质量；以覆盖更完整、发言顺序更可靠、逐字性更强、噪声/遗漏更少的一侧作为正文主源，另一侧作为发言人、术语、遗漏和冲突的交叉对比材料。联网核验只确认实体、代码、术语和公开事实，不补写会议没有说过的内容。
- 外部核验只发送候选实体、代码、术语和必要公开事实关键词，不发送原始会议长段、发言人身份、私有链接、未公开客户/订单上下文或机密 source text。
- 三类会议分别以 `references/meeting_types/` 下的 reference 作为正文格式依据。
- 非人名存疑项统一调用 `references/verification_policy.md` 中的稳定核验 prompt。
- MAS 编排边界、触发条件、artifact schema 和自动/人工决策规则见 `references/mas_orchestration_contract.md`。
- Validator 检查格式、章节、表头、编码、样例回归，以及可选 verification 审计 artifact 的结构完整性；它不伪验证联网核验是否真实发生，也不做看好/看空等语义硬判定。

## Skill 包内容

- `skills/投资会议纪要整理`：基础 skill，定义输入归档、转录、校对、识别、编辑、排版和本地导出规则。
- 会议类型规则内置在基础 skill 中：默认 `多人复盘会`；明确单家公司专场时用 `公司交流`；明确专家问答时用 `专家交流`。
- `references/meeting_types/review_meeting.md`：多人复盘会格式依据。
- `references/meeting_types/listed_company.md`：公司交流格式依据。
- `references/meeting_types/expert_call.md`：专家交流格式依据。
- `references/mas_orchestration_contract.md`：MAS 过程自动化、专家层边界、artifact schema 和人工介入规则。
- `skills/meeting-minutes-sanitizer`：用于中文投研会议纪要脱敏。

## 运行档位

- `fast_document`：短、干净、发言人清楚、低存疑的纯文稿。文稿 -> 主流程写草稿 -> 非人名业务实体和高风险事实联网核验 -> 脚本格式验证 -> 导出。
- `standard`：普通文稿或音频+文稿。音频+文稿场景先完成音频转录，再对比音频转录、`aligned_transcript` 和文稿质量，选择质量更高的一侧写正文，另一侧用于交叉对比；随后批量本地标的候选查询，对命中风险的片段执行主流程自检和外部核验。
- `strict_audio`：音频-only、长录音、噪音重、音频/文档冲突或高风险事实密集。执行对应 readiness profile，再进入人工关口、主流程审查和本地导出。

需要主流程额外自检或 MAS 过程自动化的情况：长录音、噪音重、多人边界不清、音频与文档冲突、多标的混杂、名称/代码/数字/客户/供应商/术语存疑、最终成稿前需要补漏。短、干净、低风险的 `fast_document` 不默认启用 MAS。

## Validators

绿色检查：

```bash
python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py README.md skills/*/SKILL.md --require-cjk --portable-skill
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

MAS 手动 walkthrough 使用唯一临时目录，不作为一次性全绿 validators 清单。collector 输出必须以顶层 `ok: true` 作为继续门禁；当 `ok: false` 时，只读取 `next_action` 做补齐或修复，不消费 combined artifacts 作为有效结果：

```bash
MAS_DISPATCH="$(mktemp -d /tmp/mas-dispatch.XXXXXX)"
python3 skills/投资会议纪要整理/scripts/build_mas_task_bundle.py --request-json skills/投资会议纪要整理/references/regression_samples/mas_task_request_audio_plus_document.json --task-dir "$MAS_DISPATCH"
python3 skills/投资会议纪要整理/scripts/create_mas_source_manifest.py --request-json skills/投资会议纪要整理/references/regression_samples/mas_task_request_audio_plus_document.json --task-dir "$MAS_DISPATCH" --json
python3 skills/投资会议纪要整理/scripts/ingest_mas_artifact.py skills/投资会议纪要整理/references/regression_samples/mas_subagent_return_source_reconciliation_valid.json --task-dir "$MAS_DISPATCH" --through-phase pre_draft --json
python3 skills/投资会议纪要整理/scripts/collect_mas_artifacts.py "$MAS_DISPATCH" --through-phase pre_draft --out "$MAS_DISPATCH/mas_run_summary.json" --combined-out "$MAS_DISPATCH/mas_artifacts_collected.json" --json
python3 skills/投资会议纪要整理/scripts/plan_mas_next_action.py --summary-json "$MAS_DISPATCH/mas_run_summary.json" --json
```

`create_mas_source_manifest.py` 生成主流程自有 `source_manifest` artifact，默认只记录材料清单和未确认归档状态；真实归档状态仍由主流程确认。

`ingest_mas_artifact.py` 接收一个 subagent 返回的 JSON：有效 artifact 写入 dispatch 目录的 `artifacts/`，schema 无效或重复 artifact 写入 `repair_history/`，并返回建议的 collector 命令。

每个 subagent 返回必须携带生成 prompt 中的 `run_id`、`task_id`、`dispatch_phase` 和 `artifact_owner`，且只能返回该 task 声明的 primary/secondary artifacts。需要替换同一 run/task 的旧 artifact 时，使用 `--replace-existing`；脚本会先把旧值归档到 `repair_history/`，不允许静默覆盖。

`collect_mas_artifacts.py` 的 `mas_run_summary.json` 以顶层 `ok` 作为继续门禁，并通过 `phase_gates` 和 `next_action` 告诉主流程下一步应派发、补齐、修复、自动处理或请求人工。

`plan_mas_next_action.py` 把 collector 的 `next_action` 转成可执行清单，包括待派发 prompt、对应 ingest 命令、主流程自有 artifact 缺口、修复动作、窄口径人工确认或最终 `main_action_checklist`。

当 draft review 产生主流程修订动作时，先修改主流程自有 Markdown，再运行 `record_mas_main_actions.py --task-dir "$MAS_DISPATCH" --markdown-path NOTE.md --json`。该回执绑定当前 action 列表、Markdown SHA-256 和 source-artifact digest；回执不存在、过期或 Markdown 后续被修改时，不得进入或复用 final verification。

`run_mas_phase_operator.py` 串联 dispatch 初始化、可选自动 `source_manifest`、返回 artifact ingest、collector、combined artifacts 和 next-action plan，写入 `mas_operator_state.json`；它不启动 subagent、不写最终 Markdown，只把当前阶段停在可执行状态。`command_ok` 表示本次 operator 命令成功，`gate_ok` 表示当前 collector gate 通过，`complete` 才表示全部 phase 和最终验证已完成；自动化不得只看顶层 `ok` 判断交付完成。

`run_mas_dry_run.py` 用合成 artifact 生成阶段化 MAS 执行轨迹，用于验证 Codex subagent 派发、artifact 收集和 `next_action` 推进规则；真实会议内容仍只能由主流程写入最终 Markdown。

`mas_live_pilot_trace_synthetic.json` 记录一次可回归的真实 Codex subagent 合成试跑轨迹，覆盖 schema 修复回路和最终 `ask_user_for_narrow_confirmation` 的保守收口。

`validate_meeting_minutes_contract.py` 的规则是：必须有会议元信息和 `## 一、发言整理`；按 `会议类型` 检查对应 reference 的必要格式；只有存在真实存疑时才输出 `## 二、存疑与待确认`；存疑表必须使用固定表头并保留空白 `人工确认` 列；可靠音频模式要求正文存疑词紧跟合法 `存疑时间戳`；传入 `--require-term` / `--forbid-term` 时做样例级原文锚点保留和改写锚点拦截；传入 `--verification`/`--require-verification` 时检查非人名存疑项的旁路审计记录。Validator 只做结构校验，不判断看好/看空或主次标的。

## 隐私边界

不要提交：

- 真实会议材料、原始录音、正式纪要或私有转写。
- 外部搜索或专业数据工具查询中，不发送原始会议长段、发言人身份、私有链接、未公开客户/订单上下文或机密 source text。
- 私有绝对路径、临时审阅链接、草稿链接、token、浏览器会话数据、API key 或认证 header。
- ASR 模型权重、下载缓存、虚拟环境或本机私有配置。

公共 fixtures 必须是合成或充分脱敏内容。

## 已知限制

- 真实生产启用前仍需要脱敏 blind-run 数据验证。
- DOCX 可作为输入材料，但正式交付只导出 Markdown。
