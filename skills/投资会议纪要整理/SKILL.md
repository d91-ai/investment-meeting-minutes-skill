---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, DOCX/TXT/Markdown document, or mixed audio+text materials into source-faithful Markdown meeting minutes with a concise meeting-minutes section, a lightly cleaned reference-original section, speaker/Q&A preservation, entity correction, optional doubtful items, archiving, and local export."
---

# Investment Meeting Minutes

## Goal

Turn the current meeting's materials into one source-faithful Markdown note. The formal output contains:

- `## 一、会议纪要`: correct grammatical errors and remove contextually useless acknowledgements, procedural exchanges, oral redundancy, and repeated explanations that add no distinct information.
- `## 二、参考原文`: lightly clean filler, obvious ASR noise, meaningless repetition, and repeated false starts while preserving all source turns.
- `## 三、存疑与待确认`: include only when a name, security code, or term cannot be uniquely identified, or the current-session sources materially conflict.

Both bodies preserve real order, speaker perspective, first person, logic, uncertainty, conditions, numbers, timing, actions, and every distinct fact. The final Markdown is written and modified only by the main workflow.

## Core boundaries

- Use only current-session audio, transcripts, documents, and user corrections as meeting-content sources. External sources may confirm only non-person names, security codes, and terminology; they must not add or rewrite meeting claims.
- Treat deletion, grammar repair, speaker boundaries, Q&A correspondence, source selection, target attribution, entity uniqueness, and fidelity as contextual model judgments. Do not replace them with keyword lists, regex allowlists, retention ratios, or deterministic semantic gates.
- A professional term, company name, code, or abbreviation is not doubtful merely because of its category. When current-session context and available evidence identify one meaning without material conflict, resolve it automatically and do not request confirmation.
- Preserve an expert's uncertainty, estimate, hearsay, inability to disclose, or need to check as meeting content. It is not a doubtful item unless the wording or identity itself is unclear.
- Same-session user corrections are high-priority evidence. An explicit self-correction such as “A，刚才说错了，是 B” resolves to B unless independent evidence still creates a real conflict.
- Never use public evidence to confirm or reject private customer relationships, orders, capacity, prices, figures, forecasts, internal progress, or other statements made in the meeting.
- If the user requests read-only analysis, no archive, no file writes, or a feasibility discussion, do not archive, export, or modify files.

## Routing by material length

Measure the selected source text before body generation.

- At or below about 12,000 Chinese characters: use `direct`. The main workflow produces both bodies in one pass. Do not start MAS or body-editor subagents.
- Above about 12,000 characters: use the long-material workflow in `references/long_material_workflow.md`.
- The 16,000-character hard limit applies to one package, never to the whole meeting. Long meetings must continue through as many packages as necessary.
- Package complete Q&A or continuous-answer groups whenever possible. A deterministic script may check order, capacity, and complete coverage, but the model decides semantic group boundaries.

## Workflow

### 1. Prepare sources

Archive raw files with `scripts/archive_raw_inputs.py` before transcription or writing unless the user has disabled writes or archive. Read `references/archive_naming_contract.md` when resolving final filenames.

Source modes:

- `document_only`: extract readable text and preserve the document's real order.
- `audio_only`: transcribe with the local audio pipeline.
- `audio_plus_document`: transcribe audio, compare both same-session sources for coverage, order, verbatimness, speaker evidence, ASR noise, omissions, and human correction, then choose the more reliable body source. Use the other source only for cross-checking.

Use UTF-8 without BOM for Markdown, TXT, JSON, CSV, TSV, and YAML. Python text I/O must pass `encoding="utf-8"`.

### 2. Transcribe audio

Use `scripts/transcribe_audio.py`: SenseVoiceSmall is the primary transcript; Paraformer-Large is auxiliary evidence for names, terms, numbers, abbreviations, and timestamps. Do not use Whisper as a fallback.

Before first use, machine changes, or production-like audio, read `references/runtime_readiness_guide.md` and run the relevant `scripts/check_investment_workflow_health.py` profile.

Timestamp rules and reliable-anchor requirements remain in `references/output_contract.md`.

### 3. Establish speaker and Q&A boundaries

Use the model to inspect short replies, mixed-speaker lines, answer-to-next-question transitions, and ambiguous labels before editing. Never mechanically assign a short reply to the previous speaker.

For long material, `scripts/build_speaker_turn_manifest.py` may parse explicit speaker labels and propose capacity-bounded packages. Review its package boundaries semantically and adjust them so a normal question and its answer remain together. The script must not clean prose or infer speaker identity.

### 4. Resolve names, codes, and terms

Follow `references/verification_policy.md`.

- Start with meeting context and user corrections.
- Query only genuinely unresolved names, codes, and terms. `scripts/query_symbol_candidates.py` supplies candidates, not final truth.
- Batch related lookups when useful. Send external tools only the candidate and necessary public aliases, never private meeting excerpts or relationship context.
- Correct a form only when current-session evidence or identity evidence makes the correction unique. Otherwise preserve the source fragment and add one `doubtful_items` record.
- Do not build a full entity inventory, candidate manifest, reason-code state machine, verification shard, or assembly receipt.

### 5. Write both bodies

Load `references/output_contract.md` and exactly one meeting-type reference:

- `references/meeting_types/review_meeting.md`
- `references/meeting_types/listed_company.md`
- `references/meeting_types/expert_call.md`

Generate both sections from the same ordered source turns.

For `会议纪要`, correct grammar and remove only content that adds no distinct information in context. Keep useful examples, reasons, conditions, comparisons, emphasis, uncertainty, numbers, timing, actions, and Q&A order. Adjacent same-speaker turns inside one continuous answer may become consecutive paragraphs; never merge different speakers.

For `参考原文`, apply only light cleanup. Keep every source turn and do not apply the stronger condensation used in `会议纪要`.

For `专家交流`, hide all speaker headings in `会议纪要` and retain only ordered bold questions with answers. Keep speaker headings and real turn boundaries in `参考原文`.

### 6. Review once against the source

Before export, perform one model-based source-fidelity review. Check:

- every substantive paragraph maps to the correct source speaker and Q&A group;
- no distinct fact, reason, condition, comparison, number, time, action, uncertainty, or negation was lost or strengthened;
- short replies and questions remain attributed correctly;
- `会议纪要` has removed genuinely useless speech without becoming a summary;
- `参考原文` remains source-aligned;
- entities and target headings follow the selected meeting-type rules.

For direct, clear material, the main workflow performs this review. Use one independent review agent only for long material or a concrete high-risk conflict. Do not create lexical diff manifests, fidelity shards, final-semantic receipts, or artifact gates.

### 7. Validate and export

Run only the checks that evaluate the current deliverable:

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 scripts/export_to_obsidian.py NOTE.md
```

`run_meeting_minutes_regression.py` is a development check for Skill or code changes. Do not run the full repository regression suite for every meeting deliverable.

The formal deliverable is Markdown. A same-stem verification sidecar is optional and only needed when genuine non-person doubtful items require an audit trail or the user explicitly requests it.

## Resources

- `scripts/archive_raw_inputs.py`: copy raw inputs into the meeting archive.
- `scripts/transcribe_audio.py`: local ASR and timestamp preparation.
- `scripts/build_speaker_turn_manifest.py`: long-material turn parsing and package planning only.
- `scripts/assemble_speaker_turn_edits.py`: validate and order long-material package returns without writing final Markdown.
- `scripts/query_symbol_candidates.py`: local security-name/code candidate lookup.
- `scripts/validate_utf8_text.py`: encoding and portable Skill checks.
- `scripts/validate_meeting_minutes_contract.py`: objective Markdown structure checks.
- `scripts/export_to_obsidian.py`: local Markdown export.
- `scripts/run_meeting_minutes_regression.py`: development-only regression.
