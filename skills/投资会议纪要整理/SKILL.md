---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, DOCX/TXT/Markdown document, or mixed audio+text materials into source-faithful Markdown meeting minutes with a concise meeting-minutes section, a lightly cleaned reference-original section, speaker/Q&A preservation, entity correction, and optional doubtful items."
---

# Investment Meeting Minutes

## Goal

Turn the current meeting's materials into one source-faithful Markdown note. The formal output contains:

- `## 一、会议纪要`: synthesize decision-relevant content into fewer, denser paragraphs. Within the same speaker turn, Q&A block, or continuous theme, merge statements that support one conclusion; retain the conclusion, key evidence, necessary causal chain, numbers, conditions, risks, actions, and uncertainty, while omitting discussion process, redundant argument, and examples that add no independent investment information.
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

## Routing by context and latency

Use `direct` when the selected source and both required bodies fit reliably in one model pass and the meeting depends on continuous global context. Use a few parallel source spans when the material has clean independent sections or complete Q&A boundaries and parallel generation will materially reduce latency. Do not route by a fixed character threshold.

Use `references/long_material_workflow.md` when one pass is unsafe or clean independent spans make parallel generation faster. Split once at natural paragraph, speaker-turn, or complete Q&A boundaries while reading the source. Do not create a separate full-source speaker-labeling copy or solve a globally optimal package plan.

## Workflow

### 1. Prepare sources

Archiving is not part of the base workflow and is never a prerequisite for transcription or writing. Handle an explicit archive request as a separate local-delivery task after the note is complete.

Source modes:

- `document_only`: extract readable text and preserve the document's real order.
- `audio_only`: transcribe once with the local SenseVoice pipeline. Use the transcript, reliable timestamp index, meeting context, and source replay for review; keep genuinely unclear wording doubtful instead of running a second full-audio ASR.
- `audio_plus_document`: extract the document immediately and run SenseVoice transcription concurrently. Compare the available same-session sources for coverage, order, verbatimness, speaker evidence, ASR noise, omissions, and human correction, then choose the more reliable body source. Use the other source only for cross-checking.

Use UTF-8 without BOM for Markdown, TXT, JSON, CSV, TSV, and YAML. Python text I/O must pass `encoding="utf-8"`.

### 2. Transcribe audio

Use `scripts/transcribe_audio.py`: SenseVoiceSmall produces the transcript and fsmn-vad provides the global short-segment timeline. Do not run a second full-audio ASR. Resolve names, terms, numbers, and abbreviations from current-session context and permitted identity evidence; preserve genuinely unclear audio as doubtful. Do not use Whisper as a fallback.

Before first audio use or after a machine change, run `python3 scripts/transcribe_audio.py --check-model-cache`. This is a deployment check, not a per-meeting gate.

Timestamp rules and reliable-anchor requirements remain in `references/output_contract.md`.

### 3. Establish speaker and Q&A boundaries

Use the model to inspect short replies, mixed-speaker lines, answer-to-next-question transitions, and ambiguous labels before editing. Explicit speaker labels are evidence, not an input requirement. When labels are absent, infer only the turn or Q&A boundary supported by discourse context and use conservative `发言人1/2/...` labels when identity is uncertain. Never mechanically assign a short reply to the previous speaker or invent an identity.

When long material has reliable explicit labels, `scripts/build_speaker_turn_manifest.py` may parse them and make a linear capacity-bounded package plan. Pass model-confirmed names through `--known-speaker` when the source uses named labels. When labels are missing or unreliable, do not create a second full-source working copy: infer conservative anonymous turns while processing each source span and preserve label continuity across adjacent spans. A normal question and its answer should remain together. Do not add a deterministic speaker classifier or infer personal identity in the script.

### 4. Resolve names, codes, and terms

Follow `references/verification_policy.md`.

- Do not finalize a doubtful item from package-local context. For each candidate, inspect every occurrence and the relevant surrounding passages across the current meeting before deciding.
- Start with meeting context and user corrections. Close anything that the current session identifies uniquely, including obvious ASR forms constrained by nearby explanations, numbers, or parallel terms.
- For a still-unresolved public company, institution, security code, product, technology, model, or abbreviation, use available local candidates or a targeted external lookup before adding it to `doubtful_items`. Batch the small unresolved set when useful. `scripts/query_symbol_candidates.py` supplies candidates, not final truth. Send external tools only the candidate and necessary public aliases, never private meeting excerpts or relationship context.
- Give every candidate exactly one internal verdict from `references/verification_policy.md`. Only `genuinely_doubtful` may enter `doubtful_items`; the other verdicts must close the candidate without an ambiguity row.
- Correct a form only when current-session evidence or identity evidence makes the correction unique. A `genuinely_doubtful` item keeps the source fragment and adds one `doubtful_items` record.
- For long material, each package candidate must carry its exact source fragment, `package_id` and `turn_id` or another reliable source locator, and the minimum surrounding context. The main workflow uses those locators to close the candidate across its current-meeting occurrences in memory; this is a targeted candidate review, not a second full-source rewrite or a new artifact.
- Do not build a full entity inventory, candidate manifest, reason-code state machine, verification shard, or assembly receipt.

### 5. Write both bodies

Load `references/output_contract.md` and exactly one meeting-type reference:

- `references/meeting_types/review_meeting.md`
- `references/meeting_types/listed_company.md`
- `references/meeting_types/expert_call.md`

Generate both sections from the same ordered source turns.

For `会议纪要`, write a materially shorter investment-research information layer rather than a lightly edited transcript. Aggregate one conclusion with its key evidence, necessary causal chain, conditions, risks, actions, numbers, and uncertainty. Retain research signals and follow-up variables even when they do not yet support an immediate decision. Omit rhetorical setup, conversational exploration, repeated support, and examples that merely illustrate an already supported point; keep an example only when it adds an independent fact, boundary, or counterexample. Preserve speaker attribution and Q&A order, and never merge different speakers. Do not target a fixed compression ratio.

For `参考原文`, apply only light cleanup. Account for every source turn and do not apply the stronger condensation used in `会议纪要`. A wholly meaningless turn may produce no body text; record a short omission reason only in a long-material package where an entire source turn is removed. The direct path needs no additional artifact or gate.

`参考原文` is still a model-edited readable body, not a direct copy of the input. Remove meaningless speech and filler, repair obvious grammar and sentence breaks, and omit visible source timestamps in the same model pass. Do not add a separate timestamp-cleaning stage when the model can ignore them directly.

For `专家交流`, hide all speaker headings in `会议纪要` and retain only ordered bold questions with answers. Keep speaker headings and real turn boundaries in `参考原文`.

### 6. Review where the text is generated

For `direct`, check the generated bodies against the source in the same main workflow. For long material, each package performs this check for its own source span before returning body-ready text. Check:

- every substantive paragraph maps to the correct source speaker and Q&A group; every entity, number, causal claim, or conclusion newly present in the output must map back to the current-session source. External identity evidence may normalize a public name or code but must not supply a meeting fact;
- `参考原文` lost or strengthened no distinct substantive fact, reason, condition, comparison, number, time, action, uncertainty, or negation;
- `会议纪要` lost or strengthened no research-relevant conclusion, key evidence, quantitative anchor, condition, assumption, risk, contrary view, follow-up variable, action, uncertainty, or negation that changes the interpretation boundary;
- short replies and questions remain attributed correctly;
- `会议纪要` is materially more concise and aggregated than `参考原文`, without dropping a decision-relevant conclusion, qualification, risk, or contrary view or adding an organizer's conclusion;
- `参考原文` remains source-aligned;
- entities and target headings follow the selected meeting-type rules.

After long-material assembly, the main workflow only fixes ordering, meeting-type formatting, adjacent-span continuity, globally unique entity spelling, and explicitly flagged conflicts. It must not reread and semantically re-review the full source, rewrite completed packages, or launch an independent full-source reviewer. For a concrete high-risk conflict, reopen only the relevant source span plus the minimum neighboring context and change it only when that evidence supports the change. Do not create lexical diff manifests, fidelity shards, final-semantic receipts, or artifact gates.

### 7. Validate and deliver

Run only the checks that evaluate the current deliverable:

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
```

Handle an explicit Obsidian or other local-export request as a separate delivery task. Development regressions live outside the installed Skill and are never a per-meeting gate.

The formal deliverable is Markdown. Do not create a verification sidecar in the base workflow; handle a separately requested audit artifact as a separate task.

## Resources

- `scripts/transcribe_audio.py`: local ASR and timestamp preparation.
- `scripts/build_speaker_turn_manifest.py`: optional explicit-turn parsing and linear long-material package planning.
- `scripts/assemble_speaker_turn_edits.py`: order one current-format return per source turn without writing final Markdown; write the full process JSON to `--out` and return only a compact coverage summary on stdout.
- `scripts/query_symbol_candidates.py`: local security-name/code candidate lookup.
- `scripts/validate_utf8_text.py`: encoding and portable Skill checks.
- `scripts/validate_meeting_minutes_contract.py`: objective Markdown structure checks.
