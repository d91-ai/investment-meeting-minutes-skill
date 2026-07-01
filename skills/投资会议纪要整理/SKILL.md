---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, or mixed audio+text input into a strict Markdown meeting note with speaker segmentation, company-name correction, stock-symbol validation, source-file archiving, Subagent-assisted intermediate drafts and omission checks, and Markdown/Word export. Triggers include: 整理会议录音, 整理投资会议纪要, 把这段转录整理成纪要, 输出 Obsidian 会议纪要, 校对公司名称和股票代码, 导出 md 和 word, 结合录音与文字整理, 按发言人/版块分段."
---

# Investment Meeting Minutes

## Overview

Produce a strict Chinese investment meeting note from the current meeting's audio, transcript, document, or mixed materials. The final body is a speaker-by-speaker cleaned transcript: preserve each speaker's original order, viewpoint, pronouns, logic, uncertainty, and meaningful wording; only remove pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts. Validate names and stock codes before writing confirmed entities, and export the human-confirmed Markdown + Word note.

Use the fastest safe path for the source risk. The default path is a single final writer with deterministic checks; enable Subagents only when the source is long, noisy, conflict-heavy, multi-target, or fact-sensitive. Write final Markdown, Word, and archive outputs only through the main workflow.

## Stable Contract

- Workflow after input archive: 转录 -> 校对 -> 识别 -> 联网核验 -> 编辑 -> 排版 -> 验证. Do not silently skip any step; when a step is not applicable, record `skipped_reason`, and when a step fails, record the failure reason and safest next action.
- Source boundary: use only current-session materials as meeting-content sources. External sources must verify non-person business entities, codes, terms, and high-risk public facts before they are written as confirmed; they must not add meeting content.
- ASR: use local SenseVoiceSmall as the primary transcript model and Paraformer-Large as auxiliary proofreading plus timestamp evidence when available. Do not switch to Whisper or another ASR. If the local ASR/timestamp chain cannot run, first diagnose and repair model cache, dependencies, device compatibility, memory, or chunking; use a text-only path only when the runtime cannot be restored and the user accepts that audio review is incomplete.
- Final writer: Subagents may produce intermediate notes, candidate blocks, verification notes, and omission findings; they must not directly write final deliverables. Subagent dispatch follows an any-risk trigger: one qualifying risk condition is enough to trigger the relevant reviewer; multiple conditions only raise priority.
- Run profile: prefer `fast_document` for short, clean document-only sources; use `standard` for ordinary meetings; use `strict_audio` for long audio, audio/document conflicts, or high-risk facts.
- Meeting type: default to `多人复盘会`. Use `公司交流` only for a single-company special meeting. Use `专家交流` only for expert Q&A. Do not create `其他`.
- Output format: follow `references/output_contract.md` for shared structure, ambiguity-table columns, and Word style; follow the matching meeting-type reference for body structure: `references/meeting_types/review_meeting.md`, `references/meeting_types/listed_company.md`, or `references/meeting_types/expert_call.md`.
- Speaker headings: identify speaker titles from current-session context when the source provides enough evidence, such as self-introduction, moderator address, agenda role, Q&A role, or stable transcript labels. Write the identified name or role as the `###` heading. If a speaker cannot be identified reliably, keep the fallback heading as `### 发言人1`, `### 发言人2`, `### 发言人3`, etc. in actual first-appearance order.
- Doubtful items: use one internal `doubtful_items` list as the source for verification, final table rows, and any same-stem verification sidecar. Keep final table columns in `references/output_contract.md`; keep process details in `references/verification_policy.md`.
- Fidelity: `## 一、发言整理` is a source-aligned cleaned transcript by speaker, not a content summary, abstract, rewrite, interpretation, or third-person retelling. Preserve source perspective and pronouns such as `我`、`我们`、`个人觉得`; do not rewrite them into `发言人认为`、`专家表示`、`管理层表示`、`公司表示` unless those words appear in the source. The only allowed cleanup is deleting pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts.
- Validators: keep validation to encoding, Markdown/Word structure, and regression samples. Do not add content-direction validators, semantic-consistency hard checks, or Subagent-output validators.

## Workflow

### Choose run profile

- `fast_document`: use for short, clean document-only material with clear speakers and few/no uncertain entities. Skip Subagents and ASR readiness checks, but do not skip the mandatory live verification pass for any non-person business entity or high-risk fact written as confirmed. Run local formatting validators before export.
- `standard`: use for ordinary document-only or audio-plus-document work. Batch local entity/code candidate lookup first, then run mandatory live verification before confirmed writing; use Subagents only for triggered risk areas.
- `strict_audio`: use for audio-only, long/noisy meetings, audio/document conflicts, or high-risk facts. Run the relevant readiness profile before the expensive step.

Before final writing, create a process-only dispatch record. Do not write this record into the final note body. Record:
- `transcript_auditor`: triggered / not triggered, triggered_by list, skipped_reason when not triggered.
- `content_integrity_reviewer`: triggered / not triggered, triggered_by list, skipped_reason when not triggered.
- When a subagent is unavailable, record the failure and whether the main workflow completed equivalent checks.

### 0. Prepare Inputs

Archive raw files before transcription or writing. Use `scripts/archive_raw_inputs.py`; read `references/archive_naming_contract.md` before changing archive/export naming or archive bridges.

Handle source modes:
- `audio_only`: archive, then transcribe with SenseVoice.
- `document_only`: archive, then arrange speaker turns from the provided text/document without summarizing, rewriting, or changing viewpoint.
- `audio_plus_document`: archive both; use both as evidence and keep conflicts visible.

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

Trigger the Transcript Auditor Subagent when any one of these conditions is true: audio is long, noise is heavy, multiple-speaker boundaries are unclear, audio and document evidence conflict, or timestamp alignment is important for doubtful-item review.

### 3. Correct names and symbols

Use references only when they match the uncertainty:
- `references/verification_policy.md`: ASR cleanup, speaker naming, company names, stock-code lookup, evidence boundaries, stable doubtful-item prompt, target roles, investment actions, and heading coverage.

Rules:
- Start from meeting context before choosing a company, ticker, term, customer, supplier, number, date, or event.
- Confirm company names and stock codes before writing them as facts, following `references/verification_policy.md`. Local candidates and ASR output are clues, not proof.
- Run live/network verification as a non-skippable process for non-person business entities and high-risk facts before final writing. If the live source, network, or required professional source is unavailable, do not mark the item confirmed; keep source wording, record the failure path in process notes or sidecar, and place unresolved business items in `## 二、存疑与待确认`.
- Batch local candidate lookup before live verification when several names appear, for example `scripts/query_symbol_candidates.py --batch-file terms.txt --json`. Use `a-stock-data` live sources and reliable external sources required by `references/verification_policy.md`; use `scripts/query_symbol_candidates.py` only as a candidate generator.
- Use this process-only verification prompt before final writing: "For each non-person business entity, ticker, term, customer/supplier, number, date, event, or investment action that will be written as confirmed, first match it to current-session source context, then verify it through at least one reliable live/network or professional external evidence path from `references/verification_policy.md`. If evidence is conflicting, insufficient, unavailable, or not unique, preserve the source wording, mark the doubtful fragment, and keep it in `doubtful_items`; do not add conclusions not present in the meeting source."
- Build and verify `doubtful_items` with the fields, type values, person/business split, and sidecar rules in `references/verification_policy.md`. If a non-person item cannot be confirmed, keep the source wording, mark the doubtful fragment, and keep it in the list for `## 二、存疑与待确认`.
- For audio/video or timestamped transcript sources, locate each doubtful fragment against `timestamp_index.json` before writing `## 二、存疑与待确认`. Use `HH:MM:SS-HH:MM:SS` only when the fragment matches a timestamped sentence/phrase or a short `source=sensevoice_vad_segment`, `duration_ms <= 10000` record. If the source is text/document-only or no reliable audio anchor exists, use the no-timestamp table shape and do not write a timestamp column.
- Do not estimate ambiguity timestamps from the relative position of cleaned notes, summaries, or edited paragraphs.
- Derive final rows and any `.verification.json` or `.verification.jsonl` only from `doubtful_items`; if they conflict, fix the shared list and regenerate both artifacts instead of adding validator hard rules.
- Ignore pure person-name uncertainty unless it changes an investment fact or attribution.

Trigger the Content Integrity Reviewer Subagent when any one of these conditions is true: multiple targets are mixed, target attribution is complex, high-risk facts appear, non-person business doubtful items are numerous, or omission risk is high.

### 4. 编辑

Write one final speaker-ordered note. Use `references/output_contract.md` plus the matching meeting-type reference.

Preserve actual speech order, speaker perspective, original logic, uncertainty, and meaningful wording for every meeting type. The final body may only remove pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts; it must not summarize, rewrite, interpret, merge separate turns, polish into research-report prose, or change first-person wording into third-person attribution. Do not convert the note into a summary, compressed brief, research-report section, conclusion list, or target summary table. If a speaker appears multiple times, keep later turns in their real position. Do not include workflow debugging fields such as `输入来源`, `整理说明`, tool names, logs, paths, temporary workflow links, temporary identifiers, or draft-stage explanations.

Before export, do a source-fidelity pass against the current-session transcript or document:
- For each substantive paragraph, confirm it maps back to a source span from the same speaker turn.
- Preserve first-person and speaker-perspective wording when the source uses it; do not recast it into third-person attribution.
- Keep long answers as lightly cleaned ordered prose. Split for readability only when the source naturally changes topic; do not replace them with `主要包括`、`核心观点`、`总结来看` style summaries, and do not add connective analysis that the speaker did not say.
- If Subagent notes are more compressed than the source, use them only as omission or risk findings and write final prose from the source span.
- Run a heading self-check against `output_contract.md` and the selected meeting-type reference. The final body must not contain contract-escape headings such as `发言片段`、`未归类`、`主题整理`、`内容摘要`、`观点汇总`; 多人复盘会 must not use a theme name as a fake speaker heading.

### 5. 排版

After final Markdown confirmation, validate and export locally. Do not skip transcription, proofreading, identification, editing, formatting, export, or validation silently; if one step is not applicable or cannot complete, record `skipped_reason` or the failure reason in the process notes before continuing or reporting the blocker.

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 scripts/export_to_obsidian.py NOTE.md
```

The exporter writes one Markdown file and one Word file. Do not generate PDF. If Word export fails, keep local Markdown output and report the failure without deleting files.

PDF input is not a baseline parsing capability. Archive PDF files only as attachments, or ask the user to provide readable text extracted outside this skill.

## Reference Routing

- Shared output structure, ambiguity tables, and Word style: `references/output_contract.md`.
- Meeting-type references: `references/meeting_types/review_meeting.md`, `references/meeting_types/listed_company.md`, and `references/meeting_types/expert_call.md`.
- Archive/export naming: `references/archive_naming_contract.md`.
- Runtime readiness: `references/runtime_readiness_guide.md`.
- Name/code/entity proofreading, evidence boundaries, target attribution, and doubtful-item verification prompt: `references/verification_policy.md`.
- Subagent workflow and failure handling: `references/subagent_guide.md`.

## Resources

Core scripts:
- `archive_raw_inputs.py`: copy current raw files into the workflow archive.
- `transcribe_audio.py`: local SenseVoiceSmall transcription plus Paraformer auxiliary proofreading and available timestamp-index preparation; no Whisper fallback.
- `process_transcript.py`: transcript cleanup aid.
- `query_symbol_candidates.py`: local symbol candidate lookup.
- `export_to_obsidian.py` / `save_to_my_obsidian.sh`: final Markdown + Word export.
- `validate_utf8_text.py`, `validate_meeting_minutes_contract.py`, `run_meeting_minutes_regression.py`: encoding, Markdown/Word formatting, and sample-regression checks.

## Output Contract

Every final note must follow `references/output_contract.md`, including metadata, speaker-order preservation, heading rules, meeting-type formatting, ambiguity-table shape, and Word style.

If the user asks for optimization later, preserve this simplified structure unless they explicitly request a breaking change.
