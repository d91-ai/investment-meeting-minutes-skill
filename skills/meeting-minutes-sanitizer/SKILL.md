---
name: meeting-minutes-sanitizer
description: Sanitize Chinese investment or research meeting minutes by removing speaker identities, attribution, timestamps, direct-quote style, and personal speaking style while preserving business facts. Use when the user provides .md, .txt, or converted meeting-minutes text and asks to 脱敏, 去发言人, 去发言风格, generate RAG 入库文件, or output both DOCX and JSON/JSONL.
---

# Meeting Minutes Sanitizer

## Overview

Use this skill to turn Chinese investment or research meeting minutes into two artifacts from the same sanitized topic units:

- `<input-stem>_sanitized.docx` for human review.
- `<input-stem>_rag.jsonl` for RAG ingestion.

The core rule is: preserve business facts as much as possible, and remove speaker traces as much as possible.

## Quick Start

Run the bundled script on Markdown or plain text input:

```bash
python3 scripts/sanitize_minutes.py path/to/minutes.md
```

Optional flags:

```bash
python3 scripts/sanitize_minutes.py path/to/minutes.txt --output-dir outputs --meeting-date 2026-06-14
```

The script currently supports `.md` and `.txt`. For `.docx` input, first convert the document to Markdown or plain text, then run the script.

## Output Naming

Preserve the original input file stem and add suffixes with underscores, not dots:

- `原文件名_sanitized.docx`
- `原文件名_rag.jsonl`

For example, `2026-06-14 - 晚间科技材料与市场多人复盘会.md` should produce:

- `2026-06-14 - 晚间科技材料与市场多人复盘会_sanitized.docx`
- `2026-06-14 - 晚间科技材料与市场多人复盘会_rag.jsonl`

## Processing Rules

Delete or neutralize:

- Markdown speaker headings such as `### 张三`, `### 王总`, and `### 某老师`.
- Speaker names, speaker identity, role, institution, location, resume, and affiliation when used as speaker identity.
- Attribution such as `某某认为`, `某某表示`, `发言人A说`, `张三提到`.
- Fillers, catchphrases, jokes, dialect, first-person phrasing, and strong personal speaking style.
- Direct quote punctuation while preserving the underlying business fact where possible.
- Raw timestamps.
- Personal names in meeting titles or scheduler names.

Preserve:

- Company names, stock names, stock codes, customer names, products, orders, prices, percentage changes, capacity, yield, delivery, mass-production or validation progress, technology routes, industry-chain judgments, market judgments, review logic, risk judgments, and business items still requiring confirmation.

Important distinction:

- Preserve a company when it is the analyzed business object.
- Delete or generalize a company only when it is merely the speaker identity source.
- Do not over-summarize concrete facts. Keep facts such as `A公司 Q2 订单下修 30%` and `PCB 涨价 20%` concrete.

## Output Contract

Create `<input-stem>_sanitized.docx` with this structure:

1. Title: `脱敏会议纪要`
2. `一、文档信息`
   - `会议日期`
   - `会议类型`
   - `脱敏等级：L2_FACT_PRESERVED`
   - `处理说明：已删除发言人身份和发言风格，保留业务事实`
3. `二、主题纪要`
   - Each unit starts with `主题：...`
   - Content is neutralized business facts and judgments.
4. `三、待确认业务事项`
   - Keep only business-related uncertain items.
   - Do not keep speaker names or raw timestamps.

Create `<input-stem>_rag.jsonl` with one JSON object per topic chunk:

```json
{
  "chunk_id": "meeting_YYYYMMDD_unit_001",
  "text": "主题：AI/PCB涨价｜PCB产业链。AI 相关炒作进入更重视业绩兑现的阶段。PCB 行业存在涨价线索，木林森已发出涨价 20% 的函，铜及上游材料上涨对 PCB 价格形成支撑。",
  "metadata": {
    "source_type": "meeting_minutes",
    "meeting_date": "2026-06-14",
    "topic": "AI/PCB涨价",
    "entities": ["PCB产业链", "木林森"],
    "speaker_identity": "removed",
    "speaker_style": "neutralized",
    "anonymization_level": "L2_FACT_PRESERVED",
    "business_facts": "preserved"
  }
}
```

## Quality Checks

Before delivering outputs, ensure:

- No `### 发言人` or Markdown speaker heading remains.
- No collected speaker name remains in sanitized text.
- No `某某认为`, `某某表示`, `发言人A`, or similar speaker attribution remains.
- No long direct quote remains.
- No raw timestamp remains.
- Every JSONL line is valid JSON.
- `<input-stem>_sanitized.docx` and `<input-stem>_rag.jsonl` come from the same sanitized topic units.
