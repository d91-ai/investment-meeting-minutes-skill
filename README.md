# 投资会议纪要整理 Skill

这个仓库保存 Codex 使用的中文投资会议纪要整理 Skill。它把投资研究场景中的录音、转写稿、DOCX/TXT/Markdown 文稿或音频+文稿材料，整理为一份来源忠实的 Markdown 纪要。

## 输出

正式 Markdown 包含：

- `会议纪要`：修正语病，删除无信息寒暄、程序性对话、口语赘余和无新增信息的重复表达。
- `参考原文`：由模型去除口水词和无意义信息、理顺文本；不得直接复制输入。所有来源轮次必须被处理，长材料包对整轮删除记录理由。
- `存疑与待确认`：仅在名称、代码、术语或来源听写无法唯一识别时出现。

两份正文都保留真实发言顺序、问答边界、人称、判断、条件、数字、时间、动作和不确定性。最终 Markdown 只由主流程生成和修改。

## 运行方式

1. 读取文稿；有音频时使用本地 SenseVoiceSmall＋fsmn-vad 完成一次转录与时间轴准备，不再对整场音频运行第二套 ASR。
2. 音频和文稿同时存在时，由主流程比较覆盖、顺序、逐字性、噪声、遗漏和人工修正后选择正文源。
3. 根据上下文耦合和延迟选择路线：强连续上下文且容量允许时直接生成；存在清晰独立板块或完整 Q&A 边界时可用少量并行段。不使用固定字数路由。
4. 分段只发生在自然段、发言轮次或完整 Q&A 之间。脚本只做线性容量分包，不生成全文预标注副本，也不求全局最优。
5. 无发言人标签时，基模在生成正文的同一次处理中判断保守的匿名轮次。
6. 每段完成来源对照后直接定稿；主流程只处理接缝、格式、统一实体写法和具体冲突，不再进行第二轮全文语义复核。
7. 存疑候选先检查其在整场会议中的全部出现位置和相关上下文；仍未解决的公开身份问题先做定向查询，查询后仍不唯一才进入存疑。
8. 运行 UTF-8 和 Markdown 客观结构校验。归档和 Obsidian 导出仅在用户明确要求时执行。

语病修正、无用信息、重复、短回复归属、问答边界、实体唯一性、标的归因和原意漂移都由模型结合上下文判断。脚本不使用关键词或硬规则替代这些语义判断。

## 主要文件

- `skills/投资会议纪要整理/SKILL.md`：核心工作流与边界。
- `references/output_contract.md`：共同 Markdown 格式。
- `references/meeting_types/`：多人复盘会、公司交流、专家交流格式。
- `references/verification_policy.md`：名称、代码、术语核验边界。
- `references/long_material_workflow.md`：仅长材料加载的轻量分包流程。
- `scripts/transcribe_audio.py`：本地 ASR。
- `scripts/build_speaker_turn_manifest.py`：显式 turn 解析与线性容量分包。
- `scripts/assemble_speaker_turn_edits.py`：长材料返回的顺序和覆盖检查。
- `scripts/validate_meeting_minutes_contract.py`：客观 Markdown 结构校验。
- `tools/meeting_minutes/`：不进入安装 Skill 的可选归档、导出和历史迁移工具。

## 开发验证

以下检查用于修改 Skill、脚本或输出格式之后，不作为每份会议纪要的生产门禁：

```bash
python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py README.md skills/投资会议纪要整理 --recursive --portable-skill
python3 tests/meeting_minutes/run_regression.py --json
```

验证具体纪要：

```bash
python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --json
```

首次使用音频或机器环境改变后，只检查本地模型缓存：

```bash
python3 skills/投资会议纪要整理/scripts/transcribe_audio.py --check-model-cache
```

## 隐私边界

不要提交真实会议材料、录音、正式纪要、私有转写、临时链接、token、会话数据、模型权重或本机配置。外部核验只发送候选名称、代码、术语和必要公开别名，不发送会议原文或未公开业务上下文。公共 fixtures 必须是合成或充分脱敏内容。
