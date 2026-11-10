# Artifact Steward · 文件与版本管家

一个可迁移的 Agent Skill：检查重复文件和迭代版本，判断哪些能删、哪些应留，在授权范围内分类、清理、恢复并核验。

A portable Agent Skill for evidence-based duplicate cleanup, version review, file organization and verified recovery. Python 3.11+, standard-library core, no model API or background service required.

## 能做什么

| 能力 | 实际行为 |
| --- | --- |
| 跨位置查重 | 完整SHA256比较，区分重复内容和不同路径的独立用途 |
| 版本判断 | 结合真实交付、最后要求和实物内容，不单凭“最终版”或日期选稿 |
| 逐项删留 | 给出保留、建议处理或证据不足及其理由、保留件和引用依据 |
| 分类 | 按项目和用途生成方案，复核冲突与引用后再移动 |
| 应用保护 | 保护已知聊天附件、WPS及代理/Git管理目录，允许补充本地保护范围 |
| 精确执行 | 默认预览；按真实授权、精确路径、身份和完整摘要执行 |
| 核验与恢复 | 保留件前后校验、逐项日志、同卷恢复区、不覆盖恢复、异常后对账 |

完整内容相同，不代表任意一个位置都能删除：两个项目引用的同一图片要分别保留；微信附件应保留其原入口；WPS缓存不能成为唯一持久备份。

## 安装与使用

下载 [最新技能包](https://github.com/Ring0321/artifact-steward/releases/latest)，或克隆本仓库。完整技能目录包含 `SKILL.md`、`scripts/`、`references/`、`assets/` 和 `agents/`；保持这些相对位置。

- **Codex**：将完整目录作为 `artifact-steward` 放入当前安装使用的 skills 目录，通常为 `~/.codex/skills/artifact-steward/`。
- **WorkBuddy**：在“技能 → 添加技能 → 上传技能”导入发布ZIP。入口来源见 [官方技能文档](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)。
- **DeepSeek Harness**：官方技能提供器支持项目 `.dsh/skills/`、`.agents/skills/` 等位置，目录层级见 [官方技能文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)。普通聊天客户端是否有技能和本地文件能力，需分别核实。
- **其他助手**：使用其受支持的SKILL.md导入方式；只有提示词/附件能力时按可见材料复核。

客户端接入按官方文档整理；WorkBuddy、DeepSeek桌面内导入和运行尚未实测。技能不会设置模型、账户、密钥或客户端数据库。更多边界见 [宿主接入](references/host-adapters.md)。

调用示例：

> 使用 artifact-steward 检查我指定的资料目录。把跨位置重复、不同内容旧稿、各用途最后版本分别核实，逐项说明哪些能删、为什么、保留在哪里。应用附件和项目引用保持可用，按我已经明确授权的范围执行并核验。

## 独立运行脚本

核心脚本使用Python 3.11+标准库。PDF正文检查可选使用当前环境已有的PyMuPDF或pypdf；缺失时明确报告，不自动安装。下面的路径应替换为实际授权路径，状态和报告放在源根外。

```bash
python -B scripts/steward_index.py scan --root /absolute/project --state /absolute/steward-state
python -B scripts/steward_index.py duplicates --root /absolute/project --state /absolute/steward-state --output /absolute/reports/duplicates.json
python -B scripts/steward_assess.py --review /absolute/review.json --output /absolute/reports/decisions.json
python -B scripts/inspect_documents.py --file /absolute/project/slides.pptx --output /absolute/reports/slides.json
```

Windows使用真实绝对路径并分别加引号；宿主要求RTK等命令前缀时遵守当地规则。本技能本身不依赖RTK。

判断输入见 [assessment.example.json](assets/assessment.example.json)，分类与版本配置见 [config.example.json](assets/config.example.json)。`propose_cleanup`只是声明检查范围内的建议，宿主仍须复核实际用途和动态/二进制引用；执行计划须保护输出中的保留件。生成具体计划、预览、执行、恢复及对账见 [执行说明](references/execution.md) 和 [脚本说明](references/script-guide.md)。

## 开发与验证

```bash
python -B -m unittest discover -s tests -v
python -B tools/check_release.py
python -B tools/package_skill.py --output-dir /absolute/release-output
```

测试仅操作隔离生成样本，覆盖完整查重、文档差异、授权边界、应用保护、保留件循环、恢复冲突和并发保存后的状态失效。GitHub Actions在Windows、Linux和macOS运行核心行为检查；执行结果以对应工作流记录为准。

检查器不验证全部版式、OCR、动态引用或所有软件的数据目录。执行器是协作式检查，不是抵抗恶意并发替换的操作系统沙箱，也不提供整个批次的原子事务。相同盘内移到恢复区仍占空间，永久删除不能从恢复记录还原。

欢迎通过Issue或PR提交使用问题和修复；请使用脱敏或合成样本。不要提交真实聊天、私人文件清单、索引、执行日志、账号路径或凭证。

MIT License。变更见 [CHANGELOG.md](CHANGELOG.md)。
