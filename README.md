# 投资会议纪要整理 Skill

这个仓库保存 Codex 使用的中文投资会议纪要整理 skill 包，用于把投资研究场景中的会议录音、转写稿、纪要草稿或音频+文字混合材料，整理成可人工复核、可本地归档的 Markdown 和 Word 会议纪要。

当前定位是 **risk-triggered Subagents + single final writer**：默认使用最快的单主流程路径，只有在长录音、噪音重、多人边界不清、多标的混杂、音频/文档冲突或高风险事实较多时才启用 subagent。最终 Markdown、Word 和本地归档产物只由主流程统一生成。

## 核心原则

- Subagent 可以在风险触发时输出结构化中间产物，例如发言整理候选、标的/代码清单、存疑核验清单、音频/文档冲突摘要和遗漏检查。
- 主流程是最终会议纪要的唯一写作者，避免多 agent 直接拼接造成格式、口径和风格割裂。
- 基础 skill 是唯一会议纪要 skill；会议类型只影响最终格式细节。
- 主 workflow 固定为：转录 -> 校对 -> 识别 -> 编辑 -> 排版。
- `发言整理` 默认是可复核原文纪要，不输出摘要、压缩稿或研报化改写；每段标题覆盖该段提到的全部标的，不强制一个标的一段。
- 三类会议分别以 `references/meeting_types/` 下的 reference 作为正文格式依据。
- 非人名存疑项统一调用 `references/verification_policy.md` 中的稳定核验 prompt。
- Validator 检查格式、章节、表头、Word 样式、编码、样例回归，以及可选 verification 审计 artifact 的结构完整性；它不伪验证联网核验是否真实发生。

## Skill 包内容

- `skills/投资会议纪要整理`：基础 skill，定义输入归档、转录、校对、识别、编辑、排版和本地导出规则。
- 会议类型规则内置在基础 skill 中：默认 `多人复盘会`；明确单家公司专场时用 `公司交流`；明确专家问答时用 `专家交流`。
- `references/meeting_types/review_meeting.md`：多人复盘会格式依据。
- `references/meeting_types/listed_company.md`：公司交流格式依据。
- `references/meeting_types/expert_call.md`：专家交流格式依据。
- `skills/meeting-minutes-sanitizer`：用于中文投研会议纪要脱敏。
- `.codex/agents/transcript-auditor.toml`：转写、时间戳、发言边界和音频/文档冲突 subagent。
- `.codex/agents/content-integrity-reviewer.toml`：内容完整性、标的归因、存疑核验和最终补漏 subagent。

## 运行档位

- `fast_document`：短、干净、发言人清楚、低存疑的纯文稿。文稿 -> 主流程写草稿 -> 脚本格式校验 -> 导出。
- `standard`：普通文稿或音频+文稿。先批量本地标的候选查询；只对命中风险的片段启用 subagent 或外部核验。
- `strict_audio`：音频-only、长录音、噪音重、音频/文档冲突或高风险事实密集。执行对应 readiness profile，再进入人工关口、Subagent 审查和本地导出。

优先触发 subagent 的情况：长录音、噪音重、多人边界不清、音频与文档冲突、多标的混杂、名称/代码/数字/客户/供应商/术语存疑、最终成稿前需要补漏。

## Validators

常用检查：

```bash
export INVESTMENT_MINUTES_WORKSPACE="$HOME/Documents/会议纪要整理"
# 可选：当系统 python3 没有 python-docx 时，指定已安装文档依赖的 Python
# export INVESTMENT_MINUTES_PYTHON="/path/to/python3"

python3 skills/投资会议纪要整理/scripts/validate_utf8_text.py README.md skills/*/SKILL.md --require-cjk --portable-skill
python3 skills/投资会议纪要整理/scripts/run_meeting_minutes_regression.py --json
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --require-term "我没有减仓" --forbid-term "发言人认为" --json
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --verification NOTE.verification.json --require-verification --json
python3 skills/投资会议纪要整理/scripts/validate_meeting_minutes_contract.py NOTE.md --word NOTE.docx --json
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile asr --strict
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile document
python3 skills/投资会议纪要整理/scripts/check_investment_workflow_health.py --profile export
```

`validate_meeting_minutes_contract.py` 的规则是：必须有会议元信息和 `## 一、发言整理`；按 `会议类型` 检查对应 reference 的必要格式；只有存在真实存疑时才输出 `## 二、存疑与待确认`；存疑表必须使用固定表头并保留空白 `人工确认` 列；可靠音频模式要求正文存疑词紧跟合法 `存疑时间戳`；传入 `--require-term` / `--forbid-term` 时做样例级原文锚点保留和改写锚点拦截；传入 `--verification`/`--require-verification` 时检查非人名存疑项的旁路审计记录；传入 `--word` 时同时检查 Word 导出结构和存疑词样式。

## 隐私边界

不要提交：

- 真实会议材料、原始录音、正式纪要或私有转写。
- 私有绝对路径、临时审阅链接、草稿链接、token、浏览器会话数据、API key 或认证 header。
- ASR 模型权重、下载缓存、虚拟环境或本机私有配置。

公共 fixtures 必须是合成或充分脱敏内容。

## 已知限制

- Custom agent live spawn 可能需要重启 Codex 才能加载新 `.codex/agents` 文件。
- 真实生产启用前仍需要脱敏 blind-run 数据验证。
- Word 导出依赖 `python-docx`。
