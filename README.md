# 投资会议纪要整理 Skill

这个仓库保存 Codex 使用的中文投资会议纪要整理 Skill。它把投资研究场景中的录音、转写稿、DOCX/TXT/Markdown 文稿或音频+文稿材料，整理为一份来源忠实的 Markdown 纪要。

## 输出

正式 Markdown 包含：

- `会议纪要`：修正语病，删除无信息寒暄、程序性对话、口语赘余和无新增信息的重复表达。
- `参考原文`：只做轻整理，覆盖全部来源轮次。
- `存疑与待确认`：仅在名称、代码、术语或来源听写无法唯一识别时出现。

两份正文都保留真实发言顺序、问答边界、人称、判断、条件、数字、时间、动作和不确定性。最终 Markdown 只由主流程生成和修改。

## 运行方式

1. 读取文稿；有音频时使用本地 SenseVoiceSmall 转录，Paraformer-Large 作为辅助证据。
2. 音频和文稿同时存在时，由主流程比较覆盖、顺序、逐字性、噪声、遗漏和人工修正后选择正文源。
3. 统计选定正文源的字符数。
4. 约 12,000 字以内由主流程直接生成双正文，不启用 MAS。
5. 超过约 12,000 字时按完整 Q&A 或连续发言分包；16,000 字只限制单包，不限制会议总长度。
6. 主流程合并长材料结果并对照来源做一次语义复核。
7. 运行 UTF-8 和 Markdown 结构校验后导出。

语病修正、无用信息、重复、短回复归属、问答边界、实体唯一性、标的归因和原意漂移都由模型结合上下文判断。脚本不使用关键词或硬规则替代这些语义判断。

## 主要文件

- `skills/投资会议纪要整理/SKILL.md`：核心工作流与边界。
- `references/output_contract.md`：共同 Markdown 格式。
- `references/meeting_types/`：多人复盘会、公司交流、专家交流格式。
- `references/verification_policy.md`：名称、代码、术语核验边界。
- `references/long_material_workflow.md`：仅长材料加载的轻量分包流程。
- `scripts/transcribe_audio.py`：本地 ASR。
- `scripts/build_speaker_turn_manifest.py`：长材料 turn 与建议包规划。
- `scripts/assemble_speaker_turn_edits.py`：长材料返回的顺序和覆盖检查。
- `scripts/validate_meeting_minutes_contract.py`：客观 Markdown 结构校验。
- `scripts/export_to_obsidian.py`：本地 Markdown 导出。

## 开发验证

以下检查用于修改 Skill、脚本或输出格式之后，不作为每份会议纪要的生产门禁：

```bash
python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py README.md skills/投资会议纪要整理 --recursive --portable-skill
python3 skills/投资会议纪要整理/scripts/run_meeting_minutes_regression.py --json
```

验证具体纪要：

```bash
python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --json
```

运行环境检查：

```bash
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile asr --strict
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile document
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile export
```

## 隐私边界

不要提交真实会议材料、录音、正式纪要、私有转写、临时链接、token、会话数据、模型权重或本机配置。外部核验只发送候选名称、代码、术语和必要公开别名，不发送会议原文或未公开业务上下文。公共 fixtures 必须是合成或充分脱敏内容。
