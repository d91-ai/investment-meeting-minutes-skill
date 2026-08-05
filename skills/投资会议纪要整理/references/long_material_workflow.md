# 长材料工作流

仅当选定正文源超过约 12,000 个中文字符时读取本文件。短材料由主流程直接生成双正文，不启用 MAS 或正文编辑子 Agent。

## 原则

- 12,000 字是单次双输出的目标容量；16,000 字是单包硬上限，均不是会议总长度上限。
- 优先保持完整问题、对应回答和连续回答在同一包中。
- 问答关系、短回复归属和自然语义边界由模型判断；脚本只负责字符统计、顺序和覆盖。
- 显式发言人标签不是输入前提。已有姓名标签时，主流程确认后通过 `--known-speaker` 传入；没有可靠标签时，基模先在不删改原文的工作副本中只补充保守的匿名 turn/问答边界，再交给脚本分包。
- 参考原文必须由基模完成轻整理：去除无意义信息和口水词、理顺断句与文本，不得直接引用输入；有效信息、数字、条件、不确定性、顺序和问答关系必须保留。
- 时间戳由基模在整理时直接忽略；除非时间戳造成解析失败，否则不新增时间戳清洗脚本或额外工程步骤。
- 每个包只处理被分配的连续 source turns。包内完成混合问答拆分、短回复归属和发言人衔接，返回结果应可直接按来源顺序组装。
- 子 Agent 不写最终 Markdown。主流程按全局顺序合并并完成会议类型排版。

## 执行

1. 有可靠发言人标签时，对选定的原始正文源运行 `scripts/build_speaker_turn_manifest.py`；没有可靠标签时，先由基模仅补充保守的匿名 turn/问答边界，不清理或改写原文措辞，再对该工作副本运行脚本。
2. 主流程查看 turn 与建议包边界，移动任何会拆散正常 Q&A、连续回答或上下文依赖的边界。若模型另行确认了可切包的 `allowed_break_turn_ids`，脚本只在这些合法边界中按 `ceil(total_turn_chars / target_chars)` 选择切点：先最小化最大包，再最小化各包相对等分目标（`total_turn_chars / package_count`）的总偏差。显式 `package_breaks` 仍然优先并按原规则执行。
3. 若单个完整语义组超过 16,000 字，先按自然段，再按完整句子拆分；为相邻包提供最小必要上下文，但只让一个包拥有并返回正文 turn。
4. 并行处理不同包。每个返回文件优先使用 package return schema v1.1。每个 `turn_id` 可返回多个参考原文片段和多个纪要片段；片段必须按包内来源顺序排列：

```json
{
  "schema_version": "1.1",
  "package_id": "package_001",
  "turns": [
    {
      "turn_id": "turn_0001",
      "reference_segments": [
        {"speaker_label": "分析师", "text": "轻整理后的参考原文"}
      ],
      "minutes_segments": [
        {"kind": "question", "speaker_label": "分析师", "text": "整理后的问题"},
        {"kind": "answer", "speaker_label": "专家", "text": "整理后的回答"}
      ]
    }
  ]
}
```

旧包仍可返回 `reference_text` / `minutes_text`；assembler 会将其归一为单个 `reference_segments`（使用 manifest 发言人）和单个 `paragraph` 类型的 `minutes_segments`。`reference_segments` 可为空（整轮均为无意义信息时），但必须同时提供非空 `reference_omission_reason`；新旧格式的纪要结果为空时都必须提供 `minutes_omission_reason`。存在的片段必须有非空 `speaker_label` 与 `text`；`minutes_segments` 的 `kind` 只能是 `question`、`answer` 或 `paragraph`，`speaker_label` 省略时沿用 manifest 发言人。混合问答必须为各片段返回真实或保守的发言人标签。

5. 使用 `scripts/assemble_speaker_turn_edits.py` 检查每个 `turn_id` 恰好返回一次、与 manifest 一致且按 turn 顺序组装，并输出仅供主流程处理的 v1.1 segments 工作稿。Assembler 只检查结构覆盖，不判断片段语义忠实度或包内发言归属；这些由包内模型和主流程复核。Assembler 不添加发言人标题、问题格式或标的标题，也不生成最终 Markdown。
6. 主流程根据会议类型把有序工作稿写成最终 Markdown。主流程仍进行一次全局语义复核，但只对跨包衔接、遗漏、存疑项和高风险误改做定点修正；不重复重写已经通过包内复核的整篇参考原文和纪要。
7. 只重试失败或遗漏的包，不重跑已完成包。

## 不需要的机制

- 不建立通用 task bundle、artifact envelope、collector、planner 或 phase state machine。
- 不要求子 Agent 自报 Skill SHA、run identity、owner 或 phase。
- 不建立编辑 assembly receipt、main-action receipt 或 export manifest。
- 不用关键词、固定词表、字符保留率或 lexical inventory 判断删减是否正确。
- 不为无标签文本增加确定性发言人分类器、规则词库或身份校验门禁。
- 不把开发回归测试作为单次会议的交付门禁。
