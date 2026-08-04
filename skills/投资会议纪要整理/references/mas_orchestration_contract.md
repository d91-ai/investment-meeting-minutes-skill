# MAS 编排契约

本文件定义投资会议纪要 workflow 的 MAS 目标模式。MAS 的目的不是让多个 agent 拼接终稿，而是提高端到端自动化效率、减少人工常规参与、提升最终纪要质量和可复核性。

## 目标

- 用主流程统一调度会议纪要生产线。
- 默认启用 MAS 编排，再按当前来源和内容风险增量选择 specialist agents。
- 用结构化 artifacts 承载中间判断、证据路径、风险和处理结果。
- 让人工只介入名称/代码/术语身份冲突、主源不确定或用户业务偏好不明确的异常。
- 保持最终 Markdown 只由主流程生成或修改。

## 不变边界

- 主流程是最终 Markdown、归档输出和交付口径的唯一写作者。
- Specialist agents 不直接写、拼接、改写或导出最终纪要。
- 外部资料只能确认名称、代码、术语或候选解释，不能确认或否定会议材料中的客户关系、订单、数字、预测和其他业务陈述，也不能补写会议材料没有出现的观点。
- `doubtful_items` 仍是终稿存疑表和 verification sidecar 的唯一事实源。
- Validator 保持结构、编码、样例和 artifact 字段检查，不新增看好/看空、主次标的等语义硬校验。
- 不引入 LangGraph、CrewAI、AutoGen 或其他重型 agent 框架。

## 角色

### Main Orchestrator

职责：
- 判断 run profile 和 MAS 触发条件。
- 分派 specialist agents。
- 汇总 artifacts、裁决冲突、执行低风险自动修正。
- 生成和修改最终 Markdown。
- 运行导出、validator、回归和最终交付检查。

禁止：
- 跳过转录、校对、识别、联网核验、编辑、排版或验证步骤而不记录原因。
- 把 specialist agent 的自由文本建议直接粘贴进终稿。

### Transcript Auditor

输入：
- 原始音频元信息、SenseVoice 转写、Paraformer 辅助差异、timestamp_index。

输出：
- `transcript_audit`

检查：
- ASR 噪声、长段落异常、说话人边界、SenseVoice/Paraformer 冲突、timestamp anchor 可靠性。

### Source Reconciler

输入：
- 原始音频转写、`aligned_transcript`、用户文稿、人工初审稿、同会话补充说明。

输出：
- `source_reconciliation`

检查：
- 覆盖完整度、发言顺序、逐字性、可靠时间戳、ASR 噪声、遗漏、人工校正痕迹和来源冲突。

### Entity Verifier

输入：
- 实体名称/代码/术语候选、必要别名、本地代码候选、外部核验路径。

输出：
- `entity_verification_report`
- `doubtful_items` 更新建议

检查：
- 公司名称、股票代码和行业/产品术语的身份指向。客户、供应商和竞争对手只核验名称本身，不核验会议所述关系。
- 候选发现复用 source-bound `speaker_turn_manifest` 的连续 shards，从首波并行。发现 Agent 只读本片 turns，不联网、不读历史结果/缓存，仅返回 `task_id/candidates` 紧凑观察。主流程必须验证 task 和 turn 各恰好覆盖一次，然后只按 NFKC/casefold 精确重叠做确定性合并，保留最早出现的 source surface form；禁止模糊、拼音或按完成顺序裁决。
- 主流程先从选定正文源生成并绑定真实 source SHA-256 的 main-owned `entity_candidate_manifest`。候选发现只列出身份真正不确定的名称/代码/术语，不做全量实体或术语盘点。manifest builder 以 `uncertain_only_v1` 确定性准入：每个保留项必须声明 `source_identity_unclear`、`source_conflict`、`abbreviation_ambiguous`、`local_multiple_candidates`、`local_not_found` 或 `confirmed_code_required` 中至少一个受控原因码；仅设置 `network_verification_required=true` 不能绕过该门槛。其余稳定品牌、普通行业词和无歧义提及直接剔除。若准入后没有候选，主流程不建立 manifest、不派发 Entity Verifier，并记录 `skipped_reason=no_unresolved_entity_identity`。随后精确归一重复项，并剔除产品-公司、客户-供应商、数字日期和其他会议事实 verification kinds，删除相应 `relation_ids` 与公开事实关键词。只有显式 aliases、公司-代码身份和真实 ambiguity set 可以形成不可拆 group。
- `auto` 在 candidate_count<=12、total_weight<=16、high_risk_count<=4 或只有一个 group 时保留原单任务路径；否则生成稳定权重分片。生产默认 `max_parallel=3`、`shard_target_weight=112`、`max_entity_waves=2`；收敛后的典型候选集优先装入一波 3 个并行 shard，只有权重确实超过一波容量时才进入第二波。parallel 模式至少生成 2 个 shard，且 shard 数不得超过 group 数或 `max_parallel * max_entity_waves`；任何 relation group 都不得拆分。
- `entity_candidate_manifest.policy` 必须记录 `max_parallel`（1-8）、`shard_target_weight`（1-1000000）、`max_entity_waves`（1-64）和派生的 `max_shard_count=max_parallel*max_entity_waves`。CLI 可显式覆盖这些参数以支持更大任务；非法值、容量不足的强制 parallel 计划或 policy/实际 shard 数不一致都必须失败，不能放宽覆盖、安全或 assembly gate。
- collector 将 bundle 内绑定的 `entity_verification.max_parallel` 透传给 planner；planner 的通用 `--max-parallel` 仅作 Speaker Editing 并发槽位和旧 summary 兼容回退，不得覆盖已绑定的实体分片调度策略。
- 并行任务每片使用唯一 `entity_verification_shard__shard_###` artifact key。联网输入只含候选词、必要别名和名称/代码/术语 verification kinds，不得含会议原文、关系关键词、数字、预测、私密路径或无关上下文。Agent 仅返回顶层 `task_id/results`；每个 result 仅含 `candidate_id`、`status`、`canonical_name`、`identity_key`、`evidence_paths`、`conflict_codes`、`unresolved_reason`。`input_term`、run/hash/shard 字段和完整 artifact envelope 必须由 ingest 从当前 bundle、dispatch manifest 和 candidate manifest 确定性绑定，不接受 Agent 自报。
- shard 不得产生最终 Markdown、sidecar、统一 `doubtful_items` 或最终 `entity_verification_report`。主流程仅在完整覆盖、无重叠、身份/hash/证据合法后运行确定性汇总；alias 或 identity ambiguity 冲突整体保留 unresolved，不投票也不按完成先后裁决。业务关系不得构造 dependency group 或传播冲突。
- 实体核验结果只用于身份元数据、sidecar 和存疑处置，不得驱动对发言正文的 alias/canonical 批量替换。正文默认保留 source span 原始表述；只有当前会话证据能证明明确 ASR 错听并通过 source-fidelity 复核时，主流程才可修正。
- 冷启动性能回归必须从头执行本轮外部核验，不得读取或复制历史 shard、汇总报告或同源候选核验结果并把缓存命中计入优化收益。缓存能力如需单独测试，必须另列指标，不得与冷启动用时比较。

### Target Attribution Reviewer

输入：
- 多人复盘会正文草案、来源片段、实体核验状态。

输出：
- `target_attribution_review`

检查：
- 板块行、标的行、看好/看空、客户/供应商/竞争对手/上下游误入标的行、顺带提及对象、多个标的是否共享同一逻辑链。
- 标的是否进入标题仍按当前会议上下文做语义判断；已进入标的行的每个证券标的必须与实体核验状态对齐并带非空代码，正文中凡作为证券标的出现的实体也必须带已核验代码。客户、供应商、竞争对手、可比公司或背景实体不得因此自动转成证券标的。

### Fidelity Reviewer

输入：
- 主流程确定性生成的 `fidelity_diff_manifest`，只包含 changed/at-risk spans、对应 source/draft spans 和词法 inventory 变化。
- `audio_plus_document` 额外要求已完成的 `source_reconciliation`；单一来源改用主流程已选定的正文源及选择理由，不伪造 reconciliation artifact。

输出：
- 小范围为单一 `fidelity_review_shard__shard_001`；较大范围按完整 Q&A/turn group 分成 2–3 个唯一 shard artifact。
- 主流程确定性汇总后产生唯一 `fidelity_review` 和 `fidelity_review_assembly_receipt`。无变化时不派发 specialist，由主流程生成 `mode=no_change` 的空 review 与 receipt。

检查：
- 总结化、第三人称改写、删减原因链/数字/时间/仓位动作/不确定表达、合并多轮发言、改变发言顺序。
- 确定性 inventory 只发现数字、否定、条件、日期时间、显式实体锚点和 Q&A 边界的字面变化，不自动推断未给出的语义。shard 冲突保留为 unresolved，不投票。

### Speaker Turn Editor

输入：
- 主流程从未编辑正文源生成的 `speaker_turn_manifest`。
- 一个按容量生成的连续工作包。工作包可包含多个相邻 speaker turns，但每个 turn 的发言人身份和全局顺序必须保持不变。

输出：
- 按原顺序排列的极简 JSON array；每项只含 `turn_id` 和 `edited_text`。

检查与边界：
- 仅独占整行且完整匹配的 `说话人 N`、`发言人 N`、`Speaker N` 或既有显式标签才建立 turn 边界；正文中提及这些词不得切断 turn。manifest 保留全局顺序、精确 `source_span`、文本 hash 和结构可靠性指标。
- `auto` 对短 source 保持主流程路径；对可靠解析、低噪声、可由 source 确定性无损渲染且不超过保守 direct 上限的 `document_only` 也可 direct/skip。该判断同时依赖 source mode、结构 profile 和风险，不能只看文件名或长度。显式 `full` 必须覆盖自动判断。
- 编辑前加载主流程本次使用的同一份基础 `SKILL.md`。
- 文本编辑原则只来自该基础 Skill。工作包 prompt 只定义任务范围、身份和返回结构，不复述、不替换也不扩展文本编辑要求。
- 每个已分配 `turn_id` 恰好返回一次并保持顺序；编辑 Agent 不处理 `run_id`、hash、speaker identity 等编排字段。
- 主流程以可信 dispatch task_context 补齐 `sequence`、`speaker_id`、`source_sha256` 等字段，再写入当前任务唯一的 `speaker_turn_edit__package_###` artifact。
- 不得合并、遗漏、重排或新增 turn，不得输出或修改 Markdown。

### Final Semantic Reviewer

输入：
- 主流程生成的 changed/risky span scope、对应 source spans、最终 Markdown spans，以及 doubtful/fidelity 处置。

输出：
- `final_semantic_review`

检查：
- 仅审查 changed/risky spans 与来源的语义对应，以及 doubtful/fidelity 处置是否遗漏。UTF-8、Markdown 合约、sidecar 结构、hash 和回归状态由确定性 validator/export manifest 复算，不交由 specialist 自报。

## Artifact Schema

Every non-editor specialist return uses a dispatch-bound envelope with:
- `run_id`: current dispatch run only.
- `task_id`: exact generated specialist task.
- `dispatch_phase`: task phase from the dispatch manifest.
- `artifact_owner`: generated role name.
- `artifact_type` + `artifact`, or `artifacts` for the exact primary/secondary artifact set assigned to that task.

The ingest and collector layers must reject stale-run, cross-task, cross-phase, cross-owner, unexpected, or incomplete task returns. `task_artifact_set` and `ingested_split` are reserved fields and must never appear in a returned or collected artifact; the collector derives the allowed primary/secondary set from the bound dispatch manifest. Speaker Editor returns are the only exception to the envelope at the model boundary: `ingest_mas_artifact.py --speaker-task-id TASK_ID` deterministically binds the minimal ordered response to trusted dispatch metadata before ordinary validation and transactional ingest.

`speaker_editing_mode=auto|skip|full` is a bundle-level execution decision, not an artifact schema. `auto` keeps short sources on the Main Orchestrator path and may choose deterministic direct rendering only for reliably parsed, low-noise `document_only` material without blocking editing/fidelity risks and within the conservative direct limit. It never decides from filename or length alone. Audio, unreliable structure, blocking risks, or oversized material select `full`; explicit `full` always overrides auto. `skip` explicitly selects direct rendering and produces no edit artifact or assembly receipt. `full` requires a manifest and uses one task per work package.

`speaker_turn_edit` is a shared schema, not a reusable artifact key. Every work package must have a unique safe key such as `speaker_turn_edit__package_001`; the existing one-producer-per-key rule, duplicate detection, transactional ingest, and exact task identity remain unchanged. In review-meeting source layouts with at least two standalone `××组` headings, each heading is a speaker label for the following body. The manifest preserves those speaker turns first, then packages adjacent complete turns up to a 12,000-character target and 16,000-character hard limit; package boundaries never redefine a speaker boundary. Ingest rejects missing, duplicate, foreign, or reordered turns and binds identity and hashes from trusted task context. It does not judge editing quality with character-subsequence rules, protected-word lists, filler dictionaries, retention thresholds, `removed_fillers`, or `doubtful_fragments`; editing semantics remain those of the base Skill. The collector compares the full work-package set with `speaker_turn_manifest` and requires `status=complete`.

After all edit artifacts pass, the Main Orchestrator runs `assemble_speaker_turn_edits.py`. The script assembles only by global `sequence` and creates main-owned `editing_assembly_receipt` bound to the manifest hash, current edit-artifact digest, ordered turn IDs, working-draft path, and working-draft hash. Missing or stale receipt blocks `draft_review`; replacing any edit artifact invalidates the prior receipt.

### source_manifest

Required fields:
- `source_mode`
- `materials`
- `archive_allowed`
- `archive_status`
- `skipped_reason`

`materials` must be a non-empty array for an active MAS run and must match the current bound task bundle by normalized material `kind` and basename. Known audio, document, PDF, JSON metadata, and timestamp-index filenames derive their canonical kind from the filename; an explicit mismatched `kind` cannot relabel a PDF, audio file, or metadata file as a body document. `source_mode` must match the bundle and its material-kind coverage (`audio_only`, `document_only`, or both for `audio_plus_document`). `archive_status` is one of `not_started`, `completed`, `skipped`, `skipped_for_fixture`, or `failed`; `archive_allowed=false` cannot be paired with `archive_status=completed`.

### transcript_audit

Required fields:
- `asr_primary`
- `asr_auxiliary`
- `quality_flags`
- `speaker_boundary_findings`
- `timestamp_index_status`
- `conflicts`
- `recommended_action`

### source_reconciliation

Required fields:
- `primary_body_source`
- `primary_source_reason`
- `cross_check_source`
- `coverage_findings`
- `speaker_order_findings`
- `omission_findings`
- `conflicts`
- `manual_review_required`

An automatically selected `primary_body_source` must be an eligible current-session body material name/stem or an allowed source alias such as `aligned_transcript`, `audio_transcript`, or `provided_document`; metadata, timestamp indexes, and `pdf_attachment` files are not body sources. An external URL, `file://` URI, or absolute local path is invalid. For `audio_plus_document`, automatic continuation also requires a non-empty `cross_check_source` bound to the other explicit evidence side; an empty, external, unbound, ambiguous, or same-side cross-check does not pass.

### entity_verification_report

Required fields:
- `items`
- `local_candidate_paths`
- `external_evidence_paths`
- `confirmed_item_evidence_paths`
- `confirmed_items`
- `unresolved_items`
- `conflicts`

`confirmed_item_evidence_paths` must be a per-confirmed-name/code/term mapping. Each string in `confirmed_items` must map to at least one external evidence path or source identifier. Each external reference must be a public `https://` URL or one of the supported public source IDs: `a_stock_data_live`, `cninfo`, `company_website`, `exchange_disclosure`, `professional_database`, or `regulatory_disclosure`. HTTP, localhost/private-network addresses, credential-bearing query parameters, local candidate file paths, and arbitrary opaque strings are invalid. This evidence confirms only identity or terminology, never a meeting-content claim.

In parallel mode this canonical report and `doubtful_items` are Main Orchestrator outputs, never shard outputs. `pre_draft` remains blocked until all authorized shard artifacts are complete and a current `entity_verification_assembly_receipt` binds the manifest, shard digest, report digest, doubtful-items digest, and ordered candidate IDs.

### entity_verification_shard

The Agent-facing response contains only `task_id` and `results`; every result contains exactly `candidate_id`, `status`, `canonical_name`, `identity_key`, `evidence_paths`, `conflict_codes`, and `unresolved_reason`, preserves assigned order, and exactly covers task context. Ingest then creates the canonical `entity_verification_shard__...` artifact with trusted `manifest_sha256`, `source_sha256`, `candidate_set_sha256`, `shard_sha256`, `shard_id`, `candidate_ids`, `status`, and a deterministically restored `input_term` per result. Confirmed results require legal public evidence; unresolved results require a reason. Missing, reordered, foreign, extra-control, final-report, doubtful-list, Markdown, or sidecar fields are invalid.

### entity_verification_assembly_receipt

Main-owned required fields are `manifest_sha256`, `shard_artifact_digest`, `entity_report_sha256`, `doubtful_items_sha256`, `candidate_ids`, and `status=assembled`. Replacing any shard invalidates the receipt until deterministic assembly runs again.

### doubtful_items

Use the fields and type enum in `verification_policy.md`. This list remains the only source for final ambiguity-table rows and verification sidecar records. Every entity `unresolved_items` entry and every export `known_unverified_parts` entry must have the same exact `原始表述` in `doubtful_items`. The sidecar record set must exactly match business doubtful items whose `是否需要 sidecar=true`.

### target_attribution_review

Required fields:
- `segments_reviewed`
- `wrong_grouping`
- `missing_positive_targets`
- `incidental_targets_in_heading`
- `negative_targets_in_heading`
- `non_source_companies`
- `recommended_revisions`

`segments_reviewed` must be a positive integer; a zero-scope review does not pass.

### fidelity_review

Required fields:
- `paragraphs_reviewed`
- `source_mapping_failures`
- `summary_compression_findings`
- `pronoun_rewrite_findings`
- `omission_findings`
- `recommended_revisions`

`paragraphs_reviewed` must be a positive integer; a zero-scope review does not pass.

For the main-owned `mode=no_change` path only, `paragraphs_reviewed=0` is valid when the bound diff manifest contains no changed/at-risk span and no specialist shard. In single/parallel mode, every shard has a unique `fidelity_review_shard__shard_###` key. The assembler requires exact group/span coverage with no overlap, omission, foreign span, stale source/draft/span-map hash, or identity mismatch, then emits the sole canonical review and receipt. Main actions that later modify Markdown require a narrow review of only those modified spans; the earlier draft-wide scope cannot be reused as proof for changed bytes.

### export_manifest

Required fields:
- `markdown_path`
- `markdown_sha256`
- `verification_sidecar_path`
- `validators_run`
- `regression_result`
- `export_status`
- `known_unverified_parts`
- `main_actions_verified`

`validators_run` must contain exactly the supported structural validators, `validate_utf8_text.py` and `validate_meeting_minutes_contract.py`, each with boolean `ok`. `regression_result` must contain `name=run_meeting_minutes_regression.py`, a positive integer `case_count`, and boolean `ok`; `export_status` must be `passed`, `failed`, or `blocked`. The collector resolves `markdown_path`, recomputes its SHA-256, and rejects a missing, stale, or mismatched final Markdown. When `known_unverified_parts` is non-empty, `verification_sidecar_path` must point to an existing, parseable, non-empty sidecar that passes the shared sidecar validator and matches `doubtful_items`.

### main_action_receipt

Main-owned deterministic schema 2.0 artifact, required for every final export. It is not a specialist return. `build_deterministic_export_manifest.py` recomputes and binds the exact Markdown, verification sidecar when present, current bundle/run, main-action or final-validation snapshot receipt, the two known local validator evidence files, and regression evidence. Collector re-hashes those paths and rejects missing, stale, tampered, unknown-validator, false-status, or legacy specialist manifests. The independent semantic gate is `final_semantic_review`, restricted to main-owned changed/risky spans; it does not replace deterministic validation and cannot write final Markdown.

Required fields:
- `run_id`
- `actions`
- `status=applied`
- `markdown_path`
- `markdown_sha256`
- `source_artifact_digest`

The receipt is valid only for the same run, current pre-final source artifacts, listed main actions, and exact Markdown bytes. It is a main-workflow record, not independent proof that each listed edit was semantically applied; the final writer still owns content review. Any later source-artifact or Markdown change invalidates the receipt and any existing `export_manifest`.

## MAS Default and Specialist Trigger Rules

Use MAS orchestration for every meeting-minutes run. The risk matrix controls which additional specialist agents are dispatched; it does not control whether MAS is enabled.

Add the relevant risk-specific specialists when any risk is present:
- Long audio, noisy audio, unclear speaker boundaries, or timestamp alignment risk.
- `audio_plus_document` with source conflict or unclear primary body source.
- Multiple targets, sectors, positive/negative views, customers, suppliers, competitors, or upstream/downstream entities mixed in one meeting.
- Numerous uncertain entity names, security codes, or terminology candidates.
- Company/code/term identity ambiguity, or source-fidelity risk around relationships, figures, dates, forecasts and speaker investment actions. Meeting-content claims are reviewed only against current-session source evidence; public-source availability is not a confirmation gate.
- Prior user feedback indicates summary compression, third-person rewrite, omission, missed verification, or target-attribution drift.
- A selected body source that exceeds the direct-edit context threshold. Build `speaker_turn_manifest` to enable the parallel `editing` phase. A missing manifest blocks only an explicit `full` parallel path; the Main Orchestrator may still edit directly.

For short, clean `fast_document` work with none of the above risks, keep the base main-owned artifacts and deterministic export validation; do not dispatch risk-specific specialists. If no changed/risky span exists, record that deterministic no-change state rather than dispatching a broad Contract Verifier.

For mixed audio+document work, source selection is considered unresolved until the main workflow has compared the audio-derived transcript, `aligned_transcript`, and provided document. The task request should set `source_selection_status` to one of `not_compared`, `compared_clear`, `conflict`, or `uncertain`; omitted `audio_plus_document` status, or an accidental `not_applicable`, is treated as `not_compared`. If the primary body source is already clear and no other risk exists, set `source_selection_status=compared_clear` and keep the source-quality note in the main workflow instead of dispatching source-reconciliation or other risk-specific specialists. Dispatch only the phase that is ready rather than spawning all selected specialists at once.

## Task Bundle

Before dispatching specialist agents, use `scripts/build_mas_task_bundle.py` to generate a deterministic task bundle from `run_profile`, `source_mode`, `meeting_type`, risk flags, and current-session materials.

Accepted `risk_flags` are explicit and unknown tokens fail fast:
- Audio: `audio_input`, `long_audio`, `noisy_audio`, `unclear_speaker_boundaries`, `timestamp_alignment`, `strict_audio`.
- Source reconciliation: `audio_plus_document`, `source_conflict`, `primary_source_uncertain`.
- Entity identity: `entity_verification`, `many_doubtful_items`, `company_codes`.
- Meeting-content/source review only: `high_risk_facts`, `customers_suppliers`, `numbers_dates`; these flags do not dispatch Entity Verifier unless an identity flag or bound entity manifest is also present.
- Target attribution: `target_attribution`, `multi_target`, `mixed_targets`, `positive_negative_views`.
- Fidelity: `fidelity_review`, `omission_risk`, `summary_compression`, `third_person_rewrite`, `prior_user_feedback`.
- Speaker editing: `speaker_turn_editing`, `long_transcript`, `filler_cleanup`.

Artifact selection is incremental rather than all-specialist by default. Every active MAS run keeps main-owned `source_manifest` plus final `export_manifest`; audio risks add `transcript_audit`, source-selection risks add `source_reconciliation` and `fidelity_review`, entity-identity risks add `entity_verification_report` plus `doubtful_items`, target risks add `target_attribution_review`, and fidelity risks add `fidelity_review`. Relationship, figure, date and forecast risks remain source/fidelity review concerns and do not independently create entity-verification tasks. A parallel entity manifest adds one unique shard artifact per shard plus main-owned `entity_verification_assembly_receipt`; only the Main Orchestrator produces the canonical report and doubtful list in this mode.

The task bundle must define:
- That MAS is required for the current run, plus the risk-based specialist selection.
- Expected artifacts for the selected risk profile.
- Artifact owners. `source_manifest` is created by the Main Orchestrator. `doubtful_items` may be proposed by Entity Verifier, but final handling is decided by the Main Orchestrator.
- Specialist roles, inputs, checks, required fields, JSON-only prompt, and forbidden final-output fields.
- Main-orchestrator-only responsibilities: final Markdown writing, archive/export side effects, delivery wording, and user-facing conflict decisions.
- The artifact validator command and required artifacts for later `scripts/validate_mas_artifacts.py` checks.
- A fresh dispatch `run_id` plus one `task_id` per specialist task when prompt files are materialized.

Bundle validation must enforce profile/source/meeting enums, `audio_only => strict_audio`, source-selection status, exact role task contracts, and a closed artifact-producer set covering every expected primary or secondary artifact. A non-overwrite dispatch write must recheck the target directory under the task lock and refuse any existing bundle, manifest, or generated prompt.

The task bundle is a dispatch plan, not a runtime framework. It may be used with Codex subagents when available, or as a manual task checklist when subagent execution is not available. It must not create, modify, assemble, or export final Markdown.

## Codex Subagent Dispatch Protocol

When Codex subagents are available, the Main Orchestrator may run:

```bash
MAS_DISPATCH="$(mktemp -d /tmp/mas-dispatch.XXXXXX)"
python3 scripts/build_mas_task_bundle.py --request-json REQUEST.json --task-dir "$MAS_DISPATCH"
```

For a speaker-editing run, prepare the unedited source first:

```bash
python3 scripts/build_speaker_turn_manifest.py SELECTED_TRANSCRIPT.txt \
  --out "$MAS_DISPATCH/speaker_turn_manifest.json"
python3 scripts/build_mas_task_bundle.py \
  --request-json REQUEST.json \
  --speaker-turn-manifest "$MAS_DISPATCH/speaker_turn_manifest.json" \
  --speaker-editing-mode auto \
  --task-dir "$MAS_DISPATCH"
```

For entity verification, first build the private main-owned manifest and bind it to the bundle:

```bash
python3 scripts/build_entity_candidate_manifest.py ENTITY_CANDIDATES.json \
  --out "$MAS_DISPATCH/entity_candidate_manifest.json" \
  --mode auto --max-parallel 3 \
  --shard-target-weight 112 --max-entity-waves 2
python3 scripts/build_mas_task_bundle.py \
  --request-json REQUEST.json \
  --entity-candidate-manifest "$MAS_DISPATCH/entity_candidate_manifest.json" \
  --task-dir "$MAS_DISPATCH"
```

For `audio_plus_document`, do not build this manifest while source selection is unresolved. Complete source comparison first and use `source_selection_status=compared_clear`; a manifest bound to `not_compared`, `conflict`, or `uncertain` fails closed so editors cannot clean the wrong body source.

Use a fresh dispatch directory for each meeting or pilot run. Do not reuse a prior dispatch directory with old `artifacts/` unless the collector has explicitly told you to continue that same run. Use one generated `*.prompt.md` file per specialist subagent. Each subagent should receive only its assigned prompt plus the minimum current-session source materials needed for that role. Do not pass the expected answer, prior diagnosis, or final Markdown draft unless that draft is explicitly required by the role.

Generated task files include a `dispatch_phase`:
- `pre_draft`: run after current-session source materials are prepared and before final-note drafting. Typical tasks: transcript audit, source reconciliation, entity verification.
- Entity shards in `pre_draft` use the same one-task batching and `dispatch_waves` mechanism as speaker editing. Ordinary pre-draft tasks remain independent in mixed plans. After every shard passes, run `assemble_entity_verification_shards.py`; drafting remains blocked until the collector accepts its receipt.
- `editing`: after pre-draft decisions, dispatch speaker editors governed by the same base Skill as the main workflow. `plan_mas_next_action.py --max-parallel N` assigns exactly one work package to each agent call and groups at most `N` calls into each dispatch wave. When all returns pass, the main workflow runs `assemble_speaker_turn_edits.py`; a valid assembly receipt is required before continuing.
- `draft_review`: run only after the main workflow has a draft and role-relevant source spans. Typical tasks: target attribution and fidelity review.
- `final_verification`: run only after final Markdown, sidecars, export logs, and validators exist. Typical task: contract/export verification.

Subagent execution rules:
- Dispatch one process-only specialist per generated prompt file or single-task `dispatch_batches` entry only when that task's `dispatch_phase` is ready. Each editor returns one ordered `turn_id`/`edited_text` JSON array for its assigned work package.
- Tasks in the same phase may run in parallel; tasks across phases must wait for their prerequisites.
- Keep subagents read-only toward repository files and meeting-note files unless a future task explicitly assigns a private artifact output path.
- Require each subagent to return only the JSON shape requested in its prompt. Speaker Editor prompts use the minimal array; other specialists use the dispatch-bound artifact envelope.
- Generated prompts must render the role-specific inputs and checks, use type-correct JSON examples, and state that private recordings, transcripts, meeting excerpts, and local paths must not be uploaded to external services.
- Save main-owned artifacts such as `source_manifest` under the dispatch directory's `artifacts/` folder. Use `scripts/create_mas_source_manifest.py` or `scripts/run_mas_phase_operator.py --auto-source-manifest` to create the initial `source_manifest` from the bound bundle without claiming archive completion. When `--task-dir` is present, its locked bundle and dispatch manifest are the only authority for `run_id`, source mode, and materials; request arguments cannot override them. `source_manifest` is always `pre_draft`; `main_action_receipt` is always `draft_review`.
- For each returned specialist JSON, run `scripts/ingest_mas_artifact.py RETURNED.json --task-dir "$MAS_DISPATCH" --through-phase PHASE --json`. For a Speaker Editor response, also pass the exact `--speaker-task-id TASK_ID` emitted by `plan_mas_next_action.py`; ingest adds the trusted envelope before validation. Dispatch writes and ingest commits use an exclusive task-dir lock; collection uses a shared lock. The ingest script commits a task's primary/secondary artifacts and replacement-history records as one recoverable transaction, rolls back ordinary failures, and automatically recovers an interrupted uncommitted transaction before the next ingest. Invalid or duplicate returns go to `repair_history/`.
- Require non-editor returns' `run_id`, `task_id`, `dispatch_phase`, `artifact_owner`, and artifact set to match the generated prompt and dispatch manifest. For editor returns, the main workflow supplies the exact `task_id` out of band and ingest requires exact turn coverage and order before binding the remaining identity fields. Do not ingest a return copied from another meeting or task.
- Do not manually overwrite an existing artifact file. If a specialist return is invalid or duplicate, repair or re-dispatch from the `repair_history/` record before continuing.
- When a corrected same-run/task return must replace an existing artifact, use `ingest_mas_artifact.py --replace-existing`; the old artifact must be preserved as `superseded` in `repair_history/` before replacement.
- Run `scripts/collect_mas_artifacts.py "$MAS_DISPATCH" --out "$MAS_DISPATCH/mas_run_summary.json" --combined-out "$MAS_DISPATCH/mas_artifacts_collected.json"` to merge artifacts, detect duplicates, check required artifacts from the bundle, validate field structure, produce the decision summary, and emit phase gates plus the next main-workflow action. A pending ingest transaction blocks collection until ingest recovery completes. Consume combined artifacts only when the collector summary has top-level `ok: true`; failed combined outputs are partial diagnostics.
- Run `scripts/plan_mas_next_action.py --summary-json "$MAS_DISPATCH/mas_run_summary.json" --json` to turn `next_action` into the next executable checklist: prompt files to dispatch, ingest commands to run after returns, main-owned artifact gaps, repair actions, narrow user-confirmation actions, or final `main_action_checklist`.
- Prefer `scripts/run_mas_phase_operator.py` when operating a live dispatch directory repeatedly. `--init --request-json` holds one exclusive lock while it fixes the source snapshot and creates the bundle, dispatch prompts, `source_manifest`, initial collector snapshot, and plan. Repeated `--return-json` and explicit `--return-batch-json` reduce handoffs; batch ingest is deliberately best-effort and non-atomic across tasks, while each individual task ingest remains transactional. When collector dependencies are complete, the operator may invoke the existing deterministic speaker/entity/fidelity assembler outside its own lock, then recollect and replan. It never spawns a subagent, creates a main-action receipt, or writes final Markdown.
- For a partial phase gate, pass `--through-phase pre_draft`, `--through-phase editing`, `--through-phase draft_review`, or `--through-phase final_verification` so the collector only requires artifacts whose phase is ready.
- Gate on collector top-level `ok`. Treat the embedded `decision` as actionable only when collector output is `ok: true`; otherwise repair/regenerate invalid, duplicate, or missing artifacts before final delivery.
- Apply final writing, doubtful marking, export, and user-facing decisions only in the Main Orchestrator.
- If draft-review or doubtful actions can change Markdown, apply them before `final_verification`, run `record_mas_main_actions.py` against that Markdown, then rerun collector. Dispatch only the bound changed/risky final semantic scope and build the deterministic export manifest from accepted evidence.

## Codex Operator Harness

Use `scripts/run_mas_phase_operator.py` to reduce manual command stitching during staged MAS execution. It is an operator harness, not a subagent runtime and not a final-note writer.

Initialize or inspect a dispatch directory:

```bash
python3 scripts/run_mas_phase_operator.py \
  --request-json REQUEST.json \
  --task-dir "$MAS_DISPATCH" \
  --init \
  --through-phase pre_draft \
  --json
```

After one or more specialist returns:

```bash
python3 scripts/run_mas_phase_operator.py \
  --task-dir "$MAS_DISPATCH" \
  --return-batch-json RETURN_PATHS.json \
  --through-phase draft_review \
  --json
```

`RETURN_PATHS.json` is a JSON array of explicit file paths; globs are rejected. Use repeated `--return-json` for ordinary one-by-one operation. `--no-auto-assemble` is available for diagnostic dry-runs, and `--replace-existing` remains an explicit repair authorization rather than a default.

The harness stops with an explicit `operator_status`:
- `prepare_main_owned_and_dispatch_subagents`, `create_main_owned_artifacts`, or `dispatch_subagent_tasks`: provide the listed main-owned artifacts and/or dispatch the listed prompt files.
- `repair_return_artifacts` or `repair_before_continue`: repair invalid, duplicate, or missing artifacts before continuing.
- `assemble_speaker_turns`, `assemble_entity_verification`, or `assemble_fidelity_review`: an eligible deterministic assembly was requested; under the default operator path it is run automatically and the returned state is recollected, while `--no-auto-assemble` exposes the status without running it.
- `ask_user`: ask only the narrow confirmation described by `main_actions`.
- `apply_main_actions` or `continue_main_workflow`: return to the Main Orchestrator for final drafting, doubtful handling, export, and validation.

Operator status fields have separate meanings: `command_ok` means the operator invocation completed without an internal error; `gate_ok` mirrors the collector gate for the requested phase; `complete` is true only after all required phases and final verification are complete. Do not interpret `ok=true` alone as delivery readiness.

When `main_action_checklist` appears, treat it as a Main Orchestrator runbook. It may identify source artifacts, action purpose, automation level, and output target, but it never transfers final Markdown writing or delivery wording to specialist agents.

## Privacy-safe Performance Telemetry

`scripts/mas_performance_telemetry.py` records anonymous phase/task queue/start/end, ingest, and deterministic assembly timing events through an exact allowlist schema. Records contain only enumerated `source_mode`, coarse meeting/size/risk/editing profiles, enumerated phase/task kind, aggregate candidate/group/shard/retry counts, and non-negative duration/queue measurements. They must never contain source text, meeting/run/task IDs, hashes, URLs, free-form notes, or local/private paths; unknown fields fail closed. `run_mas_phase_operator.py --telemetry-jsonl SAMPLE.jsonl` can append operator/ingest/assembly events, and synthetic dry-runs must pass `--telemetry-sample-kind synthetic`.

Each JSONL file represents one independent anonymized run sample; aggregation receives explicit sample files and does not reconstruct identities. Calibration is separated by `document_only`, `audio_only`, and `audio_plus_document`. Synthetic/non-production or incomplete samples are excluded, and each mode needs at least three complete production samples before its report may say `ready`; otherwise it must say `insufficient_data`. A ready report still sets `review_required=true` and `threshold_change_applied=false`: telemetry never changes direct/full thresholds automatically.

## Codex Dry-Run Protocol

Use `scripts/run_mas_dry_run.py` to test the staged MAS handoff before relying on live subagent execution. The dry-run builds the dispatch bundle, writes generated prompt files, emits synthetic specialist artifacts phase by phase, runs the collector after each phase, and records a `mas_dry_run_trace.json` with `next_action` after `pre_draft`, `draft_review`, and `final_verification`.

Example:

```bash
MAS_DRY_RUN="$(mktemp -d /tmp/mas-dry-run.XXXXXX)"
python3 scripts/run_mas_dry_run.py \
  --request-json references/regression_samples/mas_task_request_audio_plus_document.json \
  --artifact-fixture references/regression_samples/mas_artifacts_valid.json \
  --task-dir "$MAS_DRY_RUN" \
  --out "$MAS_DRY_RUN/mas_dry_run_trace.json" \
  --json
```

The dry-run is deterministic and uses synthetic artifacts. In a live Codex subagent pilot, replace the fixture artifact writes with actual read-only subagent JSON returns:
- Dispatch only the task files listed by the current `next_action`.
- Give each subagent the generated prompt plus the minimum role-relevant current-session materials.
- Ingest each returned JSON object with `ingest_mas_artifact.py` rather than manually copying it into `artifacts/`.
- Run `collect_mas_artifacts.py` after each phase and follow the next `next_action`.
- Dispatch the next phase without user input only when collector `ok` is true and `next_action.type` is `collect_or_dispatch_phase_artifacts`.
- Apply final automatic actions only when collector `ok` is true and `next_action.type` is `continue_without_user_intervention` or `apply_main_actions_before_final_delivery`; otherwise repair artifacts or ask the narrow confirmation requested by `next_action`.

`--overwrite` may delete only an existing MAS dry-run directory under the system temporary root whose basename starts with `mas-` and which contains a dry-run marker or prior MAS control file. A marker outside the temporary root never authorizes recursive deletion.

## Live Codex Synthetic Pilot Findings

The portable synthetic trace in `references/regression_samples/mas_live_pilot_trace_synthetic.json` records a live Codex subagent pilot with five read-only specialist tasks across `pre_draft`, `draft_review`, and `final_verification`. It used synthetic audio+document materials only; no real meeting materials, active skill install sync, commit, push, or final Markdown ownership transfer is part of the trace.

Observed behavior:

- A live Source Reconciler returned schema-invalid JSON by making `manual_review_required` a string instead of a boolean.
- `collect_mas_artifacts.py --through-phase pre_draft` caught the invalid field and emitted `next_action.type=repair_invalid_or_duplicate_artifacts`.
- After repair, the collector allowed dispatch to `draft_review`, then `final_verification`.
- With all phase artifacts present and valid, the final collector still emitted `next_action.type=ask_user_for_narrow_confirmation` because unresolved source conflicts and known unverified parts remained.

Operational rule: run collector validation after every phase, repair invalid or duplicate specialist artifacts before dispatching later phases, and never treat complete artifacts as automatic delivery when the valid `next_action` requests narrow user confirmation.

## Decision Rules

### 自动通过

The main workflow may continue without asking the user when:
- Required artifacts exist for the selected risk profile.
- Source evidence is consistent or the primary source is clearly justified.
- Entity names, security codes, and terminology normalized as confirmed have reliable identity evidence; meeting claims remain attributed source content rather than externally confirmed facts.
- `doubtful_items`, final table, and sidecar are derived from the same records.
- Fidelity review has no severe omission, perspective, or order findings.
- Contract verifier passes.

### 自动标存疑

The main workflow should keep the source wording and add or preserve `doubtful_items` when:
- Candidate entity is not unique.
- External identity evidence for the candidate name, code, or term is unavailable, insufficient, stale, or conflicting.
- Timestamp anchor is unavailable or not reliable enough for inline timestamp.
- Audio/document conflict exists but does not decide the main source.
- Target attribution is plausible but not uniquely supported.

### 修复必需

The main workflow must repair and rerun verification before final delivery when:
- Transcript audit requires a rerun or repair; this emits `repair_before_continue` in `pre_draft`, before drafting.
- Required validators were not run.
- A validator, export step, or regression result is failed, blocked, or structurally reports `ok=false`.
- The contract verifier reports errors that cannot be resolved by merely marking content doubtful.

### 请求人工

Ask the user only when:
- Same-session source conflict changes an investment fact, attribution, target heading, or source credibility.
- User correction conflicts with current-session source evidence or a name/code/term identity.
- Primary body source cannot be selected safely.
- Specialist artifacts conflict and the main workflow cannot choose a lower-risk path.

After artifacts are emitted, use `scripts/summarize_mas_decisions.py` as a conservative helper for automatic pass, automatic doubtful handling, repair-required gates, and narrow user confirmation. The helper may only consume explicit artifact fields such as `manual_review_required`, `doubtful_items`, unresolved items, known unverified parts, review findings, and export status. It must not infer semantic investment direction or target priority from free text.

Deterministic transcript/export/validator repair takes precedence over a user question in the same run. A doubtful item requests the user only when its `当前判断` or `最终处理` begins with an explicit marker such as `请求人工确认` or `请求用户确认`; free-text mentions such as `无需用户确认` do not trigger a question.

For normal runs, prefer `scripts/collect_mas_artifacts.py` over calling the validator and summarizer separately. The collector reads the task bundle, derives the required artifact list, validates the merged artifact set, detects duplicate artifact types, embeds the summarizer result, reports `phase_gates`, and emits a machine-readable `next_action` in one run summary.

When duplicate artifacts exist, collector output includes `duplicate_artifacts` with the artifact type, first path, and duplicate path. When invalid or duplicate artifacts are present, repair them before dispatching later phases, even if later-phase artifacts are also missing.

`next_action.type` is the main workflow's next executable state:
- `collect_or_dispatch_phase_artifacts`: send or recollect the listed phase task files and main-owned artifacts.
- `repair_missing_artifacts`: regenerate missing artifacts before continuing.
- `repair_invalid_or_duplicate_artifacts`: remove duplicates or regenerate invalid specialist returns.
- `repair_before_continue`: repair or rerun the transcript in `pre_draft` before drafting.
- `repair_before_final_delivery`: repair export, validator, or regression failures before final delivery; rerun the relevant checks after repair.
- `apply_main_actions_before_final_verification`: apply the listed main-owned draft/doubtful actions, record `main_action_receipt`, then rerun collector before narrow final semantic review and deterministic export-manifest construction.
- `apply_main_actions_before_final_delivery`: apply automatic doubtful, repair, or revision actions before user-facing delivery; if the action changes the final Markdown or sidecar, rerun export and validation before delivery.
- `continue_without_user_intervention`: continue the main workflow without asking the user.
- `ask_user_for_narrow_confirmation`: ask only the specific confirmation implied by valid artifacts.

## Implementation Order

1. Keep this contract as the stable reference.
2. Wire `SKILL.md`, README, and interface prompt to this contract.
3. Add synthetic regression anchors for the MAS contract and entry points.
4. Use `scripts/build_mas_task_bundle.py` to create deterministic specialist dispatch plans.
5. Use `scripts/validate_mas_artifacts.py` for lightweight artifact field validation once artifacts are emitted.
6. Use `scripts/summarize_mas_decisions.py` to turn valid artifacts into automatic pass, automatic doubtful handling, or user-confirmation decisions.
7. Use `scripts/collect_mas_artifacts.py` as the default handoff layer from subagent JSON files to a validated run summary.
8. Use `scripts/run_mas_dry_run.py` to verify staged phase handoff and trace `next_action` across synthetic runs.
9. Run a fresh Codex subagent synthetic blind-run through generated prompts, dispatch-bound returns, ingest, main-action receipt, final verification, and recovery before production use.
