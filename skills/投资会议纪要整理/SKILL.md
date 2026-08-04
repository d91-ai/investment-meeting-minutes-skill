---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, document, DOCX/TXT/Markdown draft, or mixed audio+text input into a strict Markdown meeting note with speaker segmentation, company-name correction, stock-symbol validation, source-file archiving, source-fidelity checks, and Markdown export. Triggers include: 整理投资会议录音, 整理投研会议录音, 整理投资会议纪要, 整理投研会议纪要, 把这段投资会议转录整理成纪要, 输出 Obsidian 投资会议纪要, 在投资会议纪要中校对公司名称和股票代码, 导出投资会议纪要 md, 结合录音与文字整理投资会议纪要, 按发言人/版块分段整理投资会议纪要."
---

# Investment Meeting Minutes

## Overview

Produce a strict Chinese investment meeting note from the current meeting's audio, transcript, document, or mixed materials. The final body is a speaker-by-speaker cleaned transcript: preserve each speaker's original order, viewpoint, pronouns, logic, uncertainty, and meaningful wording; only remove pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts. Validate names and stock codes before writing confirmed entities, and export the human-confirmed Markdown note.

Use MAS orchestration by default for every meeting-minutes run, with specialist agents selected incrementally from the actual source and content risks. Write final Markdown and archive outputs only through the main workflow. A same-stem `.verification.json` or `.verification.jsonl` may be kept as an internal audit sidecar, but it is not a formal deliverable.

Use `references/mas_orchestration_contract.md` as the process-automation contract. MAS is an execution and review layer; it does not mean dispatching every specialist for every meeting, and it is not a second writer. Specialist agents produce only the structured artifacts selected for the current risk profile, while the main workflow remains responsible for decisions, final writing, export, and validation.

## Stable Contract

- Workflow after input archive: 转录 -> 校对 -> 识别 -> 名称/代码/术语核验 -> 编辑 -> 排版 -> 验证. Do not silently skip any step; when a step is not applicable, record `skipped_reason`, and when a step fails, record the failure reason and safest next action.
- Entity candidate discovery begins from the source-bound `speaker_turn_manifest` shards in the first parallel wave. Discovery workers read only assigned turns, do not use the network or historical results, and return compact `task_id/candidates` observations. The Main Orchestrator requires exact task/turn coverage and merges only exact NFKC/casefold overlaps while preserving the earliest observed source form; fuzzy, pinyin, and completion-order matching are forbidden.
- Parallel Entity Verifier workers return only compact `task_id/results` semantic evidence. They do not self-report the original term, run identity, hashes, shard metadata, or artifact envelope; deterministic ingest reconstructs those fields from the current bundle, dispatch manifest, and candidate manifest and rejects missing, reordered, foreign, or extra fields.
- MAS boundary: use MAS orchestration by default, following `references/mas_orchestration_contract.md`; select specialist agents only when their base or risk-specific artifact is required. For a selected body source, build a hashed `speaker_turn_manifest` before editing. A standalone full-line `说话人 N`、`发言人 N` or `Speaker N` label starts a turn; the same words inside body prose do not. In review-meeting material with at least two standalone `××组` headings, each heading starts a distinct speaker turn and stays attached to its following body. `speaker_editing_mode=auto` keeps short sources on the main path and may also choose direct rendering for a reliably parsed, low-noise `document_only` source that can be rendered losslessly; the decision is never based only on filename or length. Blocking editing/fidelity risks, audio modes, unreliable structure, or an oversized source keep `full`; an explicit `full` always overrides auto. `skip` is an explicit direct override and requires no editor task or assembly receipt. `full` packages adjacent complete speaker turns by capacity and creates one unique `speaker_turn_edit__...` task per work package; a package may contain multiple speakers, but it must not split a heading from its body or merge speaker identities. Every editor loads the same active `SKILL.md` as the main workflow and applies its existing text-editing principles only to the assigned shard. Generated prompts define only task scope and the minimal `turn_id`/`edited_text` return structure; they must not restate, replace, or extend the Skill's text-editing instructions. Editors never handle run metadata, hashes, assembly, or final Markdown. The main workflow binds trusted task metadata, validates exact ordered turn coverage and identity, assembles accepted edits by global `sequence`, and records `editing_assembly_receipt` before draft review. Downstream drafting must consume the bundle's `working_body_contract`: direct mode consumes the bound manifest turns, while full mode consumes the assembled path/hash from the accepted receipt. It must never reopen the raw source and bypass the selected working body. Other specialists may create review artifacts, but they must not write or modify the final Markdown. Every specialist return must match the current dispatch identity and allowed artifact set.
- Entity verification scaling: before entity verification, the Main Orchestrator builds a source-hash-bound `entity_candidate_manifest` only from names, security codes, and terminology whose public identity is genuinely unresolved. Every admitted candidate must carry one or more controlled `verification_reason_codes`: `source_identity_unclear`, `source_conflict`, `abbreviation_ambiguous`, `local_multiple_candidates`, `local_not_found`, or `confirmed_code_required`. `network_verification_required=true` cannot bypass this reason requirement. Do not inventory stable brands, ordinary industry terms, or unambiguous mentions merely because they appear in the source. The builder deterministically enforces this `uncertain_only_v1` admission rule, removes customer/supplier relationships, product-company attribution, numbers, dates, orders, prices, forecasts, and other meeting-content claims from network-verification scope, and strips relationship groups and public-fact search keywords before sharding. If no candidate remains after admission, omit the entity manifest and Entity Verifier task and record `skipped_reason=no_unresolved_entity_identity`. Only explicit aliases, company/code identity links, and true ambiguity sets may remain inseparable. `auto` keeps small identity/term sets on the single Entity Verifier path and shards larger sets under the bound policy. Each shard receives only candidate terms, necessary aliases, and identity/terminology verification kinds. A shard must not output final Markdown, a sidecar, canonical `doubtful_items`, or the final entity report. The Main Orchestrator validates identity, hashes, exact coverage, evidence paths, and true identity conflicts; it then creates the sole `entity_verification_report`, sole `doubtful_items`, and `entity_verification_assembly_receipt`. Entity-verification results are process metadata, not a license to bulk-replace aliases or canonical names in speaker prose. Preserve the source surface form; only current-session evidence that clearly establishes an ASR error, followed by source-fidelity review, may support a correction in the body. A cold-start performance run must execute the current run's external verification from scratch; it must not read, copy, or count historical verification results as time savings.
- Source boundary: use only current-session materials as meeting-content sources. In `audio_plus_document`, transcribe the audio first, then compare the audio-derived `aligned_transcript` with the provided text/documents before choosing the body source. Use the higher-quality same-session source as primary, based on coverage, speaker order, verbatimness, timestamp evidence, ASR noise, omissions, and whether the text is visibly human-corrected. Use the other source as cross-check material for speaker labels, doubtful wording, omissions, and conflicts. External sources may confirm only non-person entity names, security codes, and terminology. Customer/supplier relationships, orders, capacity, prices, financial figures, dates, forecasts, internal progress, and other meeting statements are private source content: preserve them with the speaker's original uncertainty and never confirm, reject, rewrite, or mark them doubtful merely because public evidence is absent.
- User corrections: same-session user corrections or confirmations for entity names, stock codes, terms, candidates, or fact boundaries are high-priority source evidence for updating `doubtful_items` and final handling. If a name/code/term correction conflicts with original meeting materials or reliable identity evidence, record that identity conflict instead of silently overwriting. Business claims remain attributed source content and are not decided by public-source agreement.
- User override boundary: if the user explicitly asks for read-only analysis, says not to modify files, says not to archive, says this is a test run without archive, or asks to analyze feasibility before execution, do not run archive/export or other write actions. Continue read-only when possible, or ask for confirmation before writing, and record `skipped_reason=user_requested_no_archive_or_write` for skipped archive/export steps.
- ASR: use local SenseVoiceSmall as the primary transcript model and Paraformer-Large as auxiliary proofreading plus timestamp evidence when available. Do not switch to Whisper or another ASR. If the local ASR/timestamp chain cannot run, first diagnose and repair model cache, dependencies, device compatibility, memory, or chunking; use a text-only path only when the runtime cannot be restored and the user accepts that audio review is incomplete.
- Final writer: the main workflow is the only writer and reviewer for final deliverables. It must perform transcript-quality, timestamp, speaker-boundary, source-fidelity, target-attribution, doubtful-item, and omission checks before export.
- Run profile: prefer `fast_document` for short, clean document-only sources; use `standard` for ordinary document-only or ordinary audio-plus-document meetings; use `strict_audio` for audio-only, long audio, audio/document conflicts, or high-risk facts. For audio-plus-document meetings, `standard` still starts from audio transcription so the main workflow can compare source quality before selecting the primary body source.
- Meeting type: default to `多人复盘会`. Use `公司交流` only for a single-company special meeting. Use `专家交流` only for expert Q&A. Do not create `其他`.
- Output format: follow `references/output_contract.md` for shared Markdown structure and ambiguity-table columns; follow the matching meeting-type reference for body structure: `references/meeting_types/review_meeting.md`, `references/meeting_types/listed_company.md`, or `references/meeting_types/expert_call.md`.
- Final filename: follow `references/archive_naming_contract.md`. Use `YYYY-MM-DD - 会议系列.md` for `多人复盘会`; resolve the series from the raw input filename against the maintained known-series list, or ask the user when no unique match exists. Use `YYYY-MM-DD - 公司名 - 上市公司交流.md` for `公司交流`, and `YYYY-MM-DD - 主题 - 专家交流.md` for `专家交流`.
- Review-meeting subsection headings: when a segment has explicit positively viewed securities targets, write the target line first and the secondary-sector line second: `#### 【标的(代码)】` followed by `##### 【二级板块】`. If no explicit positively viewed securities target exists, write only `#### 【二级板块】`. Show only the secondary/subsector name in sector lines; do not include a primary-sector prefix or `｜`, and do not output empty brackets such as `#### 【】`.
- Speaker headings: identify speaker titles from current-session context when the source provides enough evidence, such as self-introduction, moderator address, agenda role, Q&A role, or stable transcript labels. Write the identified name or role as the `###` heading. If a speaker cannot be identified reliably, keep the fallback heading as `### 发言人1`, `### 发言人2`, `### 发言人3`, etc. in actual first-appearance order.
- Doubtful items: use one internal `doubtful_items` list as the source for verification, final table rows, and any same-stem verification sidecar. Keep final table columns in `references/output_contract.md`; keep process details in `references/verification_policy.md`.
- MAS evidence boundary: `external_evidence_paths` may contain only a public `https://` URL or a supported public source ID defined in `references/mas_orchestration_contract.md`, never HTTP, localhost/private-network URLs, credential-bearing query parameters, local candidate paths, or arbitrary opaque labels. `export_manifest` is schema 2.0 and main-owned: build it only with `build_deterministic_export_manifest.py` from the exact final Markdown, receipt, sidecar, local-validator evidence, and regression evidence. A specialist self-report or legacy schema fails closed. The collector re-hashes every bound file. When `known_unverified_parts` is non-empty, the declared verification sidecar must exist, pass the shared sidecar validator, and match the business `doubtful_items` selected for sidecar before collection can succeed.
- Fidelity: `## 一、发言整理` is a source-aligned cleaned transcript by speaker, not a content summary, abstract, rewrite, interpretation, or third-person retelling. Preserve source perspective and pronouns such as `我`、`我们`、`个人觉得`; do not rewrite them into `发言人认为`、`专家表示`、`管理层表示`、`公司表示` unless those words appear in the source. The only allowed cleanup is deleting pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts.
- Validators: keep final-note validation to encoding, Markdown structure, and regression samples. Process-artifact validators may enforce structural identity, source binding, review scope, and cross-artifact set consistency, but must not infer content direction or semantic target priority.
- Do not use this skill for standalone stock-symbol lookup, generic entity cleaning, ordinary Markdown export, non-investment/non-research meeting notes, pure ASR transcription without meeting-note output, or meeting-minutes anonymization; use the relevant narrower tool or `meeting-minutes-sanitizer` when the user asks for 脱敏 / 去发言人 / RAG 入库.

## Workflow

### Choose run profile

- `fast_document`: use for short, clean document-only material with clear speakers and few/no uncertain entities. Skip ASR readiness checks, but do not skip name/code/term disambiguation when those identities are uncertain. Run local formatting validators before export.
- `standard`: use for ordinary document-only or audio-plus-document work. For audio-plus-document work, transcribe audio first, build the SenseVoice-based `aligned_transcript`, then compare it with text/documents. Choose the higher-quality same-session source as the primary body source, and use the other source to cross-check speaker labels, missing clauses, doubtful terms, and conflicts. Batch local entity/code candidate lookup first, then run mandatory live verification before confirmed writing. Run main-workflow checks for source quality, attribution, doubtful items, and omissions before export.
- `strict_audio`: use for audio-only, long/noisy meetings, audio/document conflicts, or high-risk facts. Run the relevant readiness profile before the expensive step.

Before final writing, create process-only review notes when risk is non-trivial. Keep MAS process records and any selected specialist reviews as structured artifacts defined in `references/mas_orchestration_contract.md`; low-risk runs do not dispatch risk-specific specialists. Do not write these notes into the final note body. Record transcript-quality, timestamp, speaker-boundary, audio/document conflict, target-attribution, high-risk fact, doubtful-item, and omission findings that affect the final note.

Run `scripts/build_speaker_turn_manifest.py` on the selected unedited source and pass the resulting JSON to `build_mas_task_bundle.py --speaker-turn-manifest`. The manifest records exact source spans/hashes and a conservative structure profile. The builder targets 12,000 characters per work package and enforces a 16,000-character hard limit. It identifies source-ordered speaker turns, then packages adjacent complete turns without changing speaker identity. Only an individually oversized turn may be split, first at paragraph and then sentence boundaries. Keep `speaker_editing_mode=auto` unless the main workflow has a current-session reason for an explicit direct `skip` or parallel `full` override. For `full`, use `plan_mas_next_action.py --max-parallel N`: each agent call receives exactly one work package and each wave uses at most `N` editor slots. Every editing subagent loads the same active Skill before reading its assigned source; do not send a copied summary of the editing instructions as a substitute. Its response is only an ordered JSON array of `turn_id` and `edited_text`. Ingest binds that response to the dispatch task's run metadata, speaker identity, source hashes, and sequence; editors do not reproduce those control fields. After every edit artifact has been ingested, run `scripts/assemble_speaker_turn_edits.py`; do not enter `draft_review` until the collector accepts the main-owned assembly receipt. In direct mode, deterministically render the bound manifest turns in global sequence. In full mode, consume the accepted receipt's `assembled_draft_path` and hash. Never regroup by speaker or reconstruct the draft from raw source after selecting a working-body path. The operator's `--init` path fixes the source snapshot and initial plan under one lock; its default auto-assembly only invokes these existing deterministic assemblers after collector dependencies are complete and never writes the final note.

Before semantic fidelity review, run `build_fidelity_diff_manifest.py` against the selected source, the assembled/direct working body, and an explicit source-to-draft span map. Its deterministic inventory flags only lexical changes in numbers, negation, conditions, date/time, explicit entity anchors, and Q&A boundaries; it does not infer semantic correctness. No-change stays main-owned. Otherwise review only complete changed/at-risk Q&A/turn groups through one small-task shard or 2–3 unique shards, then require `assemble_fidelity_review_shards.py` to verify exact coverage, hashes, identity, and receipt. When draft-review artifacts require main-workflow changes, apply those changes before `final_verification`, then use `scripts/record_mas_main_actions.py` to bind the applied action list to the current Markdown SHA-256 and source-artifact digest. Even when no semantic action is required, final export uses a main-owned validation snapshot receipt. Dispatch only the independently scoped final semantic review for spans changed by main actions; deterministic validators run locally. Then build `export_manifest` from their persisted evidence. Do not reuse an older manifest after Markdown, sidecar, receipt, bundle, or evidence changes; explicit replacement preserves repair history.

### 0. Prepare Inputs

Archive raw files before transcription or writing unless the user explicitly requested no archive/no file writes/read-only analysis. Use `scripts/archive_raw_inputs.py`; read `references/archive_naming_contract.md` before changing archive/export naming or archive bridges.

Handle source modes:
- `audio_only`: archive, then transcribe with SenseVoice.
- `document_only`: archive, then arrange speaker turns from the provided text/document without summarizing, rewriting, or changing viewpoint.
- `audio_plus_document`: archive both and transcribe audio first. Compare the audio-derived `aligned_transcript` with text/documents for coverage, speaker order, verbatimness, timestamp evidence, ASR noise, omissions, and human-correction signals. Write from the higher-quality same-session source; use the other source for speaker identity, term correction, omission detection, and conflict review. If sources disagree, keep the wording from the source with clearer same-session evidence; unresolved conflicts stay in process notes or `doubtful_items`.

Keep Chinese text files and generated Markdown/TXT/JSON/YAML as UTF-8 without BOM. In Python text I/O, pass `encoding="utf-8"`. If UTF-8 decoding fails or replacement characters appear, stop and report the affected file.

### 1. 转录

When audio is provided, use `scripts/transcribe_audio.py` for local SenseVoiceSmall primary transcription, Paraformer-Large auxiliary cross-checking, and timestamp-index preparation. Runtime failures are repair targets, not a reason to skip audio evidence by default.

Default audio pipeline:
1. Run full-audio fsmn-vad once to obtain global VAD segment boundaries, then transcribe each VAD segment with SenseVoiceSmall as the primary ASR transcript.
2. Run Paraformer-Large as an auxiliary ASR cross-check for finance terms, company names, stock codes, numbers, English abbreviations, and timestamp evidence.
3. Do not automatically replace the SenseVoiceSmall transcript with Paraformer-Large output. Use Paraformer differences as proofreading evidence, and surface unresolved conflicts in `transcript_audit` or `suspect_confirmation`.
4. Build a near-verbatim `aligned_transcript` from the SenseVoice primary transcript plus confirmed cross-check corrections. Do not use cleaned meeting-note prose for timestamp alignment.
5. Prefer sentence/phrase anchors when available. A short `source=sensevoice_vad_segment`, `precision=segment`, `duration_ms <= 10000` record is also reliable enough for doubtful-item replay. Other segment/chunk/minute-level ranges are not reliable final doubtful timestamps.
6. Use the selected timestamp index as the timestamp source for ambiguity rows, preserving `source` and `precision` fields.

Timestamp-index rules:
- Do not use Whisper for transcription, fallback transcription, cross-checking, or timestamp generation.
- Do not run VAD separately on pre-cut 20s/60s chunks for final timestamps; VAD boundaries must come from one full-audio VAD pass.
- `batch_size_s=60` is a runtime generation parameter, not a promise to preserve 60-second chunk artifacts.
- `timestamp_index.json` entries should include `start`, `end`, `start_ms`, `end_ms`, `duration_ms` when known, `chunk_index`, `text`, `source`, and `precision` when the engine exposes them.
- `source` should distinguish `paraformer`, `sensevoice`, `sensevoice_paraformer_checked`, `fa_zh_forced_alignment`, and fallback segment sources when applicable.
- `precision` should distinguish `sentence`, `phrase`, `segment`, `chunk`, and `unavailable`.
- For ambiguity rows, use the same internal `doubtful_items` list for verification, inline timestamps, and the final table. First match the doubtful term to `timestamp_index.text`; output `HH:MM:SS-HH:MM:SS` only from sentence/phrase anchors or short `sensevoice_vad_segment` records. If no reliable match exists, use the no-timestamp table shape and do not write a timestamp placeholder.
- Model downloads, dependency installation, and first-cache warmup are setup work, not formal transcription time.
- Before first use, machine changes, or production-like audio, read `references/runtime_readiness_guide.md` and run `scripts/check_investment_workflow_health.py --profile asr --strict`. Use `--runtime-smoke` only when a real short-audio service call is needed.

### 2. 校对

Use `scripts/process_transcript.py` when text is long, noisy, or missing clear speaker boundaries. Correct obvious ASR noise and delete only pure filler words, meaningless repetitions, and repeated false starts while preserving the speaker's viewpoint, pronouns, order, uncertainty, judgment strength, numbers, timing, actions, and meaningful wording. Treat cleaned text as evidence for final writing, not as permission to summarize, rewrite, polish into report style, or change perspective.

Build a process-only speaker map before final writing. Map raw labels such as `Speaker 1` or `发言人A` to an identified name or role only when current-session content supports it. Do not infer a personal name or role from topic expertise alone. When evidence is insufficient, keep numeric fallback labels in first-appearance order.

When audio is long, noise is heavy, multiple-speaker boundaries are unclear, audio and document evidence conflict, or timestamp alignment matters for doubtful-item review, the main workflow must explicitly check transcript quality, speaker boundaries, timestamp anchors, ASR conflicts, and audio/document conflicts before final writing.

Before final writing, run a source-restoration pass on the working transcript: compare each cleaned turn with its source span, restore omitted substantive clauses, and keep examples, reasons, hedge words, conditions, numbers, time points, actions, and speaker uncertainty unless they are clearly filler or ASR noise. If an intermediate draft is shorter or more polished than the source span, treat it as a checklist for omissions only and rewrite the paragraph from the source span.

### 3. Correct names and symbols

Use references only when they match the uncertainty:
- `references/verification_policy.md`: ASR cleanup, speaker naming, company names, stock-code lookup, evidence boundaries, stable doubtful-item prompt, target roles, investment actions, and heading coverage.

Rules:
- Start from meeting context before choosing a company, ticker, term, customer, supplier, number, date, or event.
- Confirm company names and stock codes before writing them as facts, following `references/verification_policy.md`. Local candidates and ASR output are clues, not proof.
- Run live/network verification only for non-person entity names, security codes, and terminology before normalizing those identities in final writing. Public-source absence never makes a meeting-content claim doubtful.
- External verification query privacy: send only the candidate entity, ticker, term, and necessary aliases. Do not send raw meeting excerpts, speaker identities, private links, customer/order context, relation keywords, figures, forecasts, or confidential source text to external search or professional data tools.
- Batch local candidate lookup before live verification when several names appear, for example `scripts/query_symbol_candidates.py --batch-file terms.txt --json`. Use `a-stock-data` live sources and reliable external sources required by `references/verification_policy.md`; use `scripts/query_symbol_candidates.py` only as a candidate generator.
- Use this process-only verification prompt before final writing: "Verify only the identity of each non-person entity name, security code, or terminology candidate. Do not verify customer/supplier relationships, product attribution, orders, prices, numbers, dates, forecasts, internal progress, or other meeting-content claims. Lack of public evidence for a meeting claim must not create a doubtful item. If the name/code/term identity itself is conflicting, insufficient, unavailable, or not unique, preserve the source wording and keep only that exact identity fragment in `doubtful_items`."
- Build and verify `doubtful_items` with the fields, type values, person/business split, and sidecar rules in `references/verification_policy.md`. Network verification may add an item only when a name, code, or term cannot be uniquely identified; source transcription ambiguity and same-session conflicts remain separate source-review reasons.
- For audio/video or timestamped transcript sources, locate each doubtful fragment against `timestamp_index.json` before writing `## 二、存疑与待确认`. Use `HH:MM:SS-HH:MM:SS` only when the fragment matches a timestamped sentence/phrase or a short `source=sensevoice_vad_segment`, `duration_ms <= 10000` record. If the source is text/document-only or no reliable audio anchor exists, use the no-timestamp table shape and do not write a timestamp column.
- Do not estimate ambiguity timestamps from the relative position of cleaned notes, summaries, or edited paragraphs.
- Derive final rows and any internal `.verification.json` or `.verification.jsonl` sidecar only from `doubtful_items`; if they conflict, fix the shared list and regenerate both artifacts instead of adding validator hard rules. The sidecar supports audit and review, but the formal deliverable remains the Markdown note.
- Ignore pure person-name uncertainty unless it changes an investment fact or attribution.

When multiple targets are mixed, target attribution is complex, high-risk facts appear, non-person business doubtful items are numerous, or omission risk is high, the main workflow must explicitly check target attribution, high-risk claims, doubtful-item handling, heading coverage, and omissions before final writing.

For `多人复盘会`, target attribution and topic segmentation are semantic writing tasks that must be handled by the language model using current-session context, not by regexes, keyword lists, or deterministic content-direction validators:
- Each `#### 【...】` segment must be one independent theme, logic chain, or coherent comparison group. Unrelated themes must be split even when they appear in one continuous speaker turn.
- A `##### 【...】` target line may contain only securities targets with names and verified codes. It must not contain directions or sectors such as `科技｜算力`.
- In the body, every entity used as a securities target must also be written as `canonical name(verified code)`, whether or not it belongs in the target heading. Do not apply this rule to a company mentioned only as a customer, supplier, competitor, comparable, upstream/downstream entity, or background fact.
- Only explicit positively viewed targets belong in the target line. Do not promote negative, avoid/reduce, customer, supplier, competitor, comparable, upstream/downstream, background, or incidental mentions into the target line.
- If several positively viewed targets share the same sector, theme, and logic chain, they may share one target line. If their themes or logic chains differ, split them into separate segments.
- If one coherent theme contains both positive and negative targets, the segment may remain together, but the target line records only the positively viewed targets and the body preserves the negative or cautious view.
- Do not add a company to a target line unless it appears in current-session meeting materials. External evidence may confirm a name or ticker, but must not add new meeting content.
- Before export, run a model-based semantic review of topic segmentation and target attribution. If the review finds wrong grouping, missing primary positively viewed targets, target-heading entries without verified codes, body securities-target mentions without verified codes, incidental targets in headings, negative targets in target lines, or companies not present in source material, revise the Markdown body and headings before validation.

### 4. 编辑

Write one final speaker-ordered note. Use `references/output_contract.md` plus the matching meeting-type reference.

Preserve actual speech order, speaker perspective, original logic, uncertainty, and meaningful wording for every meeting type. The final body may only remove pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts; it must not summarize, rewrite, interpret, merge separate turns, polish into research-report prose, or change first-person wording into third-person attribution. Do not convert the note into a summary, compressed brief, research-report section, conclusion list, or target summary table. If a speaker appears multiple times, keep later turns in their real position. Do not include workflow debugging fields such as `输入来源`, `整理说明`, tool names, logs, paths, temporary workflow links, temporary identifiers, or draft-stage explanations.

Before export, do a source-fidelity pass against the current-session transcript or document:
- For each substantive paragraph, confirm it maps back to a source span from the same speaker turn.
- For `audio_plus_document`, map final body paragraphs back to the selected primary source first, then cross-check against the other same-session source for omissions, unclear words, speaker labels, and conflicts. If the document is selected as primary, still use audio timestamps where reliable for doubtful fragments and conflict review.
- Preserve first-person and speaker-perspective wording when the source uses it; do not recast it into third-person attribution.
- Keep long answers as lightly cleaned ordered prose. Split for readability only when the source naturally changes topic; do not replace them with `主要包括`、`核心观点`、`总结来看` style summaries, and do not add connective analysis that the speaker did not say.
- If intermediate notes are more compressed than the source, use them only as omission or risk findings and write final prose from the source span.
- Run a heading self-check against `output_contract.md` and the selected meeting-type reference. The final body must not contain contract-escape headings such as `发言片段`、`未归类`、`主题整理`、`内容摘要`、`观点汇总`; 多人复盘会 must not use a theme name as a fake speaker heading.

### 5. 排版

After final Markdown confirmation, validate and export locally. Do not skip transcription, proofreading, identification, editing, formatting, export, or validation silently; if one step is not applicable or cannot complete, record `skipped_reason` or the failure reason in the process notes before continuing or reporting the blocker.

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 scripts/export_to_obsidian.py NOTE.md
```

The exporter writes one Markdown file as the formal deliverable. Do not generate Word or PDF. If an internal verification sidecar is needed for non-person business doubts, keep it as an audit file and do not present it as the formal deliverable.

PDF input is not a baseline parsing capability. Archive PDF files only as attachments, or ask the user to provide readable text extracted outside this skill.

## Reference Routing

- Shared Markdown output structure and ambiguity tables: `references/output_contract.md`.
- Meeting-type references: `references/meeting_types/review_meeting.md`, `references/meeting_types/listed_company.md`, and `references/meeting_types/expert_call.md`.
- Archive/export naming: `references/archive_naming_contract.md`.
- Runtime readiness: `references/runtime_readiness_guide.md`.
- Name/code/entity proofreading, evidence boundaries, target attribution, and doubtful-item verification prompt: `references/verification_policy.md`.
- MAS process automation, specialist-agent boundaries, artifact schema, and automatic/manual decision rules: `references/mas_orchestration_contract.md`.

## Resources

Core scripts:
- `archive_raw_inputs.py`: copy current raw files into the workflow archive.
- `transcribe_audio.py`: local SenseVoiceSmall transcription plus Paraformer auxiliary proofreading and available timestamp-index preparation; no Whisper fallback.
- `process_transcript.py`: transcript cleanup aid.
- `build_speaker_turn_manifest.py`: split the selected unedited transcript into hashed, source-ordered speaker turns, then package adjacent complete turns by capacity without deleting filler words or changing speaker identity.
- `build_entity_discovery_plan.py`: reuse speaker-turn shards to create source-bound first-wave parallel candidate-discovery prompts without network or historical-result access.
- `assemble_entity_candidate_observations.py`: fail-closed validate compact discovery returns and deterministically merge exact normalized overlaps into candidate-manifest input plus a full turn-coverage receipt.
- `build_entity_candidate_manifest.py`: normalize and group entity candidates, choose the single or parallel path from candidate scale and risk, and generate deterministic hashed verification shards without private meeting excerpts.
- `build_fidelity_diff_manifest.py`: bind source/draft/span-map hashes, build deterministic lexical inventories, and select no-change, single, or 2–3-shard fidelity review without making semantic judgments.
- `query_symbol_candidates.py`: local symbol candidate lookup.
- `export_to_obsidian.py`: final Markdown export.
- `build_mas_task_bundle.py`: generate process-only MAS specialist task bundles and optional Codex-ready subagent prompt files before dispatch.
- `create_mas_source_manifest.py`: create the main-owned `source_manifest` artifact from the MAS request or task bundle without claiming archive completion.
- `assemble_speaker_turn_edits.py`: validate complete per-package editor returns, assemble the main-owned working body by global turn sequence, and bind it with `editing_assembly_receipt`.
- `assemble_entity_verification_shards.py`: validate complete entity-shard coverage and evidence, preserve conflicts as unresolved, and emit the sole main-owned entity report, doubtful items, and assembly receipt.
- `assemble_fidelity_review_shards.py`: validate fidelity shard identity, source/draft/span-map hashes, and exact non-overlapping group/span coverage, then emit the sole main-owned review and receipt.
- `ingest_mas_artifact.py`: receive one returned MAS subagent JSON artifact, validate it, write valid artifacts under `artifacts/`, and preserve invalid or duplicate returns under `repair_history/`.
- `collect_mas_artifacts.py`: collect returned MAS specialist JSON files from a dispatch directory, validate required artifacts, detect duplicates, report phase gates, and produce the main-orchestrator run summary with `next_action`.
- `plan_mas_next_action.py`: turn a collector `next_action` into the next executable checklist: prompt files to dispatch, ingest commands, main-owned artifact gaps, repair actions, narrow user confirmation, or final `main_action_checklist`.
- `record_mas_main_actions.py`: record main-owned pre-final actions against the exact Markdown SHA-256 and current source-artifact digest before final verification.
- `build_deterministic_export_manifest.py`: create the only accepted main-owned export manifest from re-hashed Markdown, receipt, sidecar, validator, and regression evidence; never writes the Markdown.
- `run_mas_phase_operator.py`: atomically initialize a bound dispatch/source snapshot/plan, batch-ingest explicit return paths, collect and plan, and invoke eligible deterministic assembly; it does not spawn subagents, create main-action receipts, or write final Markdown.
- `mas_performance_telemetry.py`: record exact-schema anonymous phase/task/ingest/assembly timings and aggregate them separately by source mode; it excludes synthetic/incomplete samples from calibration, reports `insufficient_data` below three complete production samples per mode, and never changes routing thresholds automatically.
- `run_mas_dry_run.py`: run a staged synthetic MAS handoff to verify prompt dispatch, artifact collection, phase gates, and `next_action` before relying on live Codex subagents.
- `summarize_mas_decisions.py`: summarize MAS artifacts into automatic pass, automatic doubtful handling, or user-confirmation decisions.
- `validate_utf8_text.py`, `validate_meeting_minutes_contract.py`, `validate_mas_artifacts.py`, `run_meeting_minutes_regression.py`: encoding, Markdown formatting, MAS artifact structure, and sample-regression checks.

Maintenance-only script:
- `organize_raw_archive_structure.py`: reorganize historical raw-input archives only after dry-run review; `--apply` and `--remove-empty-dirs` require explicit user approval and are not part of ordinary meeting processing.

## Output Contract

Every final note must follow `references/output_contract.md`, including metadata, speaker-order preservation, heading rules, meeting-type formatting, and ambiguity-table shape.

If the user asks for optimization later, preserve this simplified structure unless they explicitly request a breaking change.
