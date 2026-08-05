# 长材料工作流

仅当选定正文源超过约 12,000 个中文字符时读取本文件。短材料由主流程直接生成双正文，不启用 MAS 或正文编辑子 Agent。

## 原则

- 12,000 字是单次双输出的目标容量；16,000 字是单包硬上限，均不是会议总长度上限。
- 优先保持完整问题、对应回答和连续回答在同一包中。
- 问答关系、短回复归属和自然语义边界由模型判断；脚本只负责字符统计、顺序和覆盖。
- 每个包只处理被分配的连续 source turns，同时生成 `reference_text` 和 `minutes_text`。
- 子 Agent 不写最终 Markdown。主流程按全局顺序合并并完成会议类型排版。

## 执行

1. 对选定的未编辑正文源运行 `scripts/build_speaker_turn_manifest.py`。
2. 主流程查看 turn 与建议包边界，移动任何会拆散正常 Q&A、连续回答或上下文依赖的边界。
3. 若单个完整语义组超过 16,000 字，先按自然段，再按完整句子拆分；为相邻包提供最小必要上下文，但只让一个包拥有并返回正文 turn。
4. 并行处理不同包。每个返回文件使用以下结构：

```json
{
  "package_id": "package_001",
  "turns": [
    {
      "turn_id": "turn_0001",
      "reference_text": "轻整理后的参考原文",
      "minutes_text": "整理后的会议纪要"
    }
  ]
}
```

5. 使用 `scripts/assemble_speaker_turn_edits.py` 检查每个 `turn_id` 恰好返回一次、顺序一致且无外来 turn，并生成一个按顺序排列的 JSON 工作稿。
6. 主流程根据会议类型把有序工作稿写成最终 Markdown。Assembler 不添加发言人标题、问题格式或标的标题。
7. 主流程对最终正文与来源做一次完整语义复核；只重试失败或遗漏的包，不重跑已完成包。

## 不需要的机制

- 不建立通用 task bundle、artifact envelope、collector、planner 或 phase state machine。
- 不要求子 Agent 自报 Skill SHA、run identity、owner 或 phase。
- 不建立编辑 assembly receipt、main-action receipt 或 export manifest。
- 不用关键词、固定词表、字符保留率或 lexical inventory 判断删减是否正确。
- 不把开发回归测试作为单次会议的交付门禁。

