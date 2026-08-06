---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, DOCX/TXT/Markdown document, or mixed audio+text input into a validated Markdown meeting note. Produces a readable meeting-minutes layer plus a source-faithful proofreading layer, with speaker segmentation, entity and stock-code verification, inline doubtful-item handling, semantic review, and validated export. Triggers include 整理投资会议录音, 整理投研会议纪要, 校对公司名称和股票代码, 按多人复盘会/公司交流/专家交流输出会议纪要, and 导出投资会议纪要 Markdown."
---

# Investment Meeting Minutes

把当前会话中的投资会议音频、转写稿或文稿整理为可人工复核的 Markdown 纪要。主流程是最终 Markdown 的唯一写作者；MAS specialist 只返回过程审核 artifact。

## 三条核心原则

### 1. 所有终稿均使用“会议纪要 + 原文校对”双层结构

`# 会议纪要` 可以归纳、压缩并使用第三人称，但不得改变观点强度、条件、时间、数字、否定、因果或不确定性。`# 原文校对` 保留真实发言顺序、发言人、问答关系和第一人称，只做必要的正常编辑。摘要层的每个重要结论必须能回到校对层验证。

### 2. 正文只有标题和未解决存疑可以加粗

独立行 `**【……】**` 是段落标题；正文粗体只标记仍无法确认的最小原文片段及必要说明。经上下文和可靠证据唯一确认的转写修复、公司名和代码必须直接修正为普通字体，并退出 `doubtful_items` 和 verification sidecar。不得输出文末存疑表、候选项说明或 AI 判断。

### 3. 会议类型分别组织，不以一种问答格式覆盖全部类型

专家交流和公司交流使用“三级发言人标题 + 加粗方括号概括标题”。多人复盘会保留按发言人和标的/主题的结构；原文校对层的四级标题继续执行 `【标的(代码)| 主题】` 契约。具体格式只以 `references/output_contract.md` 为准。

## 工作流入口

1. 判断用户是只读审查还是正式整理；只读任务不得归档、导出或修改文件。
2. 读取 `references/output_contract.md`，再按会议类型读取一个 Reference：多人复盘会 `meeting_types/review_meeting.md`、公司交流 `meeting_types/listed_company.md`、专家交流 `meeting_types/expert_call.md`。
3. 按 `references/archive_naming_contract.md` 处理输入归档和文件命名。无法唯一确定会议系列、公司名或主题时向用户确认。
4. 音频输入先按 `references/runtime_readiness_guide.md` 检查环境，再使用 `scripts/transcribe_audio.py`。音频加文稿时先转录音频，比较同会话材料质量并选择正文主源。
5. 按 `references/verification_policy.md` 校对源材料、核验实体和代码；将证据充分的唯一候选直接修正为普通字体，只把仍未解决的项目写入内部 `doubtful_items`、sidecar 与正文内嵌粗体。
6. 默认按 `references/mas_orchestration_contract.md` 执行适用的 MAS 审核链。实体核验和双层忠实度审核为基线；多人复盘会增加标的归因审核；音频或 ASR 输入增加转写审核。审核必须绑定当前 Markdown 路径和 SHA-256。
7. 先生成原文校对层，再据此生成会议纪要层；不得把 specialist 的自由文本或外部背景写入任一正文层。
8. 完成摘要来源映射、顺序、遗漏、标的归因、内嵌存疑覆盖和来源忠实度复核；正文修改会使旧审核失效，必须重新审核。
9. 导出前运行：

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 scripts/run_meeting_minutes_regression.py
python3 scripts/collect_mas_artifacts.py "$MAS_DISPATCH" --through-phase draft_review --out "$MAS_DISPATCH/mas_run_summary.json" --combined-out "$MAS_DISPATCH/mas_artifacts_collected.json"
python3 scripts/export_to_obsidian.py NOTE.md --mas-summary "$MAS_DISPATCH/mas_run_summary.json"
```

正式导出会再次执行 UTF-8、主 Markdown 契约和固定回归，并要求 MAS `draft_review` 已完成、没有待落实动作，且语义审核绑定当前 Markdown 路径与 SHA-256。缺失、失败、过期、零范围或尚未落实的必要审核会阻断导出。导出后再执行 `final_verification`；最终验证未通过时不得交付。正式交付只包含最终 Markdown；verification sidecar 和 MAS artifact 是内部审核材料。

## Reference Routing

- 共享双层结构、标题和内嵌存疑：`references/output_contract.md`
- 名称、代码、证据边界、AI 判断隔离和标的归因：`references/verification_policy.md`
- 多人复盘会语义分段：`references/meeting_types/review_meeting.md`
- 公司交流正文语义：`references/meeting_types/listed_company.md`
- 专家交流正文语义：`references/meeting_types/expert_call.md`
- MAS artifact 和审核门禁：`references/mas_orchestration_contract.md`
- 输入归档与命名：`references/archive_naming_contract.md`
- 本地运行环境：`references/runtime_readiness_guide.md`

## 维护要求

修改业务或输出规则时，同时更新相关 validator、MAS prompt 和回归样例。真实会议材料、录音、私有路径、凭据和生产纪要不得进入仓库。
