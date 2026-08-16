# 长材料工作流

仅当一次上下文不能可靠完成双正文，且材料有清晰完整的发言轮次或问答边界时使用本文件。不以固定字数触发分包。

## 边界

- 主流程在首次阅读时选择少量自然边界；显式发言标签可靠时，可用 `scripts/build_speaker_turn_manifest.py` 生成线性分包。
- 问句不是自动换人或切分点。句子、完整回答或因果链跨包时，向后一包附理解所需的最小相邻上下文，恢复完整语义后只输出一次。
- 每包在同一次处理中先生成参考原文，再派生会议纪要，并对本包的事实字段和发言归属负责；分包结果不直接成为最终 Markdown。
- 子任务只标记少量可能未决、发生重要纠正或作了语境化非轻量改写的原始片段，不作跨包最终结案。主流程合并后只处理接缝、统一排版和这些已定位项目，不再全文重写。

## 分包返回格式

```json
{
  "package_id": "package_001",
  "turns": [
    {
      "turn_id": "turn_0001",
      "reference_segments": [
        {"speaker_label": "分析师", "text": "校对后的参考原文"}
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

- `turn_id` 必须与 manifest 完全一致并保持顺序。
- `minutes_segments.kind` 只能是 `question`、`answer` 或 `paragraph`。
- `candidate_fragments` 可为空；非空时 `exact_fragment` 必须原样存在于来源 turn，前后文只保留判断所需内容，不得含最终结论。
- 某份正文删除整个 turn 时，相应 segments 置空，并填写 `reference_omission_reason` 或 `minutes_omission_reason`。

## 合并

1. 用 `scripts/assemble_speaker_turn_edits.py` 按 manifest 组装，确认 turn 不重不漏。
2. 主流程按顺序修复相邻接缝并套用会议类型格式。
3. 汇总实际投资标的和少量候选，按 `verification_policy.md` 使用已读证据统一结案；批量补充已确认标的的代码，真正未决项进入存疑，重要实质纠正、代码补充和已定位的语境化非轻量改写进入修改记录，不再查询或复查全文。
4. 只重试失败的包；只有已定位的跨包风险才回看相应片段及最小相邻上下文。
