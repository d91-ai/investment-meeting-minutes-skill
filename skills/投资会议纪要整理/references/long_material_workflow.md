# 长材料工作流

当选定来源与两份正文无法在一次模型处理中可靠完成，或材料存在清晰独立板块且少量并行段能显著降低延迟时，读取本文件。不使用固定字数触发分包。

## 原则

- 问答关系、短回复归属和可切边界由模型根据上下文判断。
- 优先在自然段、显式发言人轮次或完整问答组之间切分，不追求数学上的最优均衡。
- 无可靠发言人标签时，在处理当前来源段的同一次模型调用中判断保守的匿名轮次；不另外通读全文生成一份发言人工作副本。
- 每个来源段同时生成对应的 `会议纪要` 和 `参考原文`，不再进行第二轮全文重写。
- 每个来源段在同一次处理中完成来源对照和正文定稿，对该段语义质量负责。
- 参考原文仍须去除口水词、无意义信息和显示时间戳，并理顺断句；不得直接引用输入。
- 子 Agent 只返回分配给它的来源段结果，不写最终 Markdown。最终合并和会议类型排版由主流程完成。
- 分段内只能标记少量可能存疑的原始片段，并随每项返回 exact fragment、`package_id` 和 `turn_id`（或其他可靠源位置）及最小前后文；不能依据局部上下文赋最终 verdict 或生成最终存疑表。

## 执行

1. 主流程在读取来源时直接选定少量完整语义边界。有显式发言人标签时，可用 `scripts/build_speaker_turn_manifest.py` 做线性分包；无标签时由模型直接处理当前段。
2. 当一个连续回答必须跨段时，只向后一段提供理解所需的最小上下文；正文仅由其所属段返回一次。
3. 每个分包只使用当前格式：

```json
{
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
      ],
      "candidate_fragments": [
        {"exact_fragment": "原始候选片段", "context_before": "最小前文", "context_after": "最小后文"}
      ]
    }
  ]
}
```

4. `candidate_fragments` 可为空；非空时 `exact_fragment` 必须原样存在于该 turn，`context_before` 和 `context_after` 只保留判断所需的最小相邻文本，不得包含 verdict。整个来源轮次被删除时，对空的 `reference_segments` 或 `minutes_segments` 提供对应 omission reason。不接受旧版 `reference_text` / `minutes_text`。
5. 使用 `scripts/assemble_speaker_turn_edits.py` 按 manifest 中的顺序合并当前格式返回，只检查 turn 不重不漏。
6. 主流程按顺序合并，只处理相邻段接缝、会议类型排版、全文唯一的实体写法和被明确标记的冲突；不再通读全文进行第二轮语义复核。
7. 主流程汇总并去重少量候选，按其定位回看当前会议中的全部 occurrence、模型判断的明显 ASR 变体和跨段最小相邻上下文；逐项按 `verification_policy.md` 赋予唯一 verdict。只有 `genuinely_doubtful` 进入最终 `doubtful_items`，其余三类按各自规则关闭；不得把分包局部清单直接合并为终稿。
8. 只重试失败的来源段。仅当回答跨分包或接缝已标记明确风险时，回看对应来源片段和理解所需的最小相邻上下文，定点核对数字与单位、否定、条件、主客体和动作方向；不启动全量独立复核 Agent。

## 不使用

- 固定字数路由、全文预标注副本或全局最优分包。
- task bundle、artifact envelope、collector、planner、phase state machine 或 receipts。
- 旧 package schema、语义哈希、字符保留率或词法盘点。
- 确定性发言人分类器、规则词库或身份门禁。
- 分包完成后的全文重写、全文独立复核或第二套语义审查。
