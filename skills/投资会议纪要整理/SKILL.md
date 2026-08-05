---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, DOCX/TXT/Markdown document, or mixed audio+text input into a source-faithful Markdown meeting note with speaker segmentation, entity and stock-code verification, doubtful-item handling, semantic review, and validated export. Triggers include 整理投资会议录音, 整理投研会议纪要, 校对公司名称和股票代码, 按多人复盘会/公司交流/专家交流输出会议纪要, and 导出投资会议纪要 Markdown."
---

# Investment Meeting Minutes

把当前会话中的投资会议音频、转写稿或文稿整理为可人工复核的 Markdown 纪要。主流程是最终 Markdown 的唯一写作者；MAS specialist 只返回过程审核 artifact。

## 三条核心原则

### 1. 多人复盘会只使用一种四级小段标题

保留真实发言顺序和三级发言人标题。发言人下方只使用 `references/output_contract.md` 定义的四级小段标题：有明确正向证券标的时写标的和板块，无明确证券标的时只写主题。不得虚构标的，不得输出五级标题，所有标题保持普通字体。

### 2. 正文只保留源材料，粗体只表示未解决存疑

`## 一、发言整理` 只能包含源材料中的发言以及必要的正常编辑。不得写入 AI 判断、解释、总结、推论、补充背景或处理说明。已确认的转写修复、公司名、股票代码和规范化内容使用普通字体；只有仍无法确认的最小源材料片段留在原句原位置并加粗。AI 核验内容只能进入 `doubtful_items`、verification sidecar、MAS artifact 和最终存疑表的审核列。

### 3. 公司交流和专家交流统一使用不加粗问题格式

问题使用 `【问题原文】`，下一段直接写回答。不得添加 `Q：`、`提问：`、`A：`、`回答：`、`专家回答：`等整理标签。具体格式只以 `references/output_contract.md` 为准。

## 工作流入口

1. 判断用户是只读审查还是正式整理；只读任务不得归档、导出或修改文件。
2. 读取 `references/output_contract.md`，再按会议类型读取一个 Reference：
   - 多人复盘会：`references/meeting_types/review_meeting.md`
   - 公司交流：`references/meeting_types/listed_company.md`
   - 专家交流：`references/meeting_types/expert_call.md`
3. 按 `references/archive_naming_contract.md` 处理输入归档和文件命名。无法唯一确定会议系列、公司名或主题时向用户确认。
4. 音频输入先按 `references/runtime_readiness_guide.md` 检查环境，再使用 `scripts/transcribe_audio.py`。音频加文稿时先转录音频，然后比较同会话材料质量并选择正文主源。
5. 按 `references/verification_policy.md` 校对源材料、核验实体和股票代码、建立唯一 `doubtful_items`，并严格隔离 AI 判断与发言正文。
6. 默认按 `references/mas_orchestration_contract.md` 执行适用的 MAS 审核链。实体核验和忠实度审核为基线；多人复盘会增加标的归因审核；音频或 ASR 输入增加转写审核。审核必须绑定当前 Markdown 路径和 SHA-256。
7. 主流程按真实发言顺序生成最终 Markdown，应用共享输出契约和对应会议类型 Reference，不汇总重排，不改写成研报或摘要。
8. 完成句子连贯性、遗漏、标的归因、存疑覆盖和来源忠实度复核；任何正文修改都会使旧审核失效，必须重新审核。
9. 导出前运行：

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 scripts/run_meeting_minutes_regression.py
python3 scripts/collect_mas_artifacts.py "$MAS_DISPATCH" --through-phase draft_review --out "$MAS_DISPATCH/mas_run_summary.json" --combined-out "$MAS_DISPATCH/mas_artifacts_collected.json"
python3 scripts/export_to_obsidian.py NOTE.md --mas-summary "$MAS_DISPATCH/mas_run_summary.json"
```

正式导出脚本会再次执行 UTF-8、主 Markdown 契约和固定回归，并要求 MAS `draft_review` 已完成、没有待落实动作，且语义审核绑定当前 Markdown 路径与 SHA-256。缺失、失败、过期、零范围或尚未落实的必要审核会阻断导出。导出后再执行 `final_verification`；最终验证未通过时不得向用户交付。正式交付只包含最终 Markdown；verification sidecar 和 MAS artifact 是内部审核材料。

## Reference Routing

- 共享 Markdown 结构、标题、粗体、问题和存疑表：`references/output_contract.md`
- 名称、代码、证据边界、AI 判断隔离和标的归因：`references/verification_policy.md`
- 多人复盘会语义分段：`references/meeting_types/review_meeting.md`
- 公司交流正文语义：`references/meeting_types/listed_company.md`
- 专家交流正文语义：`references/meeting_types/expert_call.md`
- MAS artifact 和审核门禁：`references/mas_orchestration_contract.md`
- 输入归档与命名：`references/archive_naming_contract.md`
- 本地运行环境：`references/runtime_readiness_guide.md`

## 维护要求

修改业务或输出规则时，同时更新相关 validator 和合成回归样例。真实会议材料、录音、私有路径、凭据和生产纪要不得进入仓库。
