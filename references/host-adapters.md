# 宿主适配

本技能的核心是文件/版本复核规则与通用 Python 辅助脚本。Skill 格式可被发现、目录可读、脚本可执行、历史可取得是四个分别核实的条件；安装技能不等于四项全部具备。工具只使用当前客户端实际开放且用户授权的能力。

## WorkBuddy

官方文档提供“专家·技能·连接器 → 技能 → 添加技能 → 上传技能”导入本地技能包的入口。导入后检查已安装列表并启用 `artifact-steward`，在对话中明确要求使用它。保留完整技能目录结构，不把脚本拆散；若使用目录安装，以当前客户端官方说明为准。

使用当前 WorkBuddy 任务可见的消息、附件及文件工具。没有核实到通用的全量聊天导出接口，不假定能直接遍历所有历史任务；不得调用不存在的 Codex 聊天工具，也不按 Codex 会话结构解析 WorkBuddy 数据库。历史不足时走目录复核模式，或处理用户通过受支持方式导出的真实记录。

来源：[技能导入](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)。客户端 UI 与目录优先级以当前安装版本为准；本发行版未做桌面内导入实测。

## 官方 DeepSeek Harness 桌面版

官方技能提供器支持 `<name>/SKILL.md` 目录包。项目级可放在 `<projectRoot>/.dsh/skills/artifact-steward/`，或受支持的 `<projectRoot>/.agents/skills/artifact-steward/`；用户级放在 `<dshHome>/skills/artifact-steward/`。`dshHome` 使用该安装的实际配置，不硬编码盘符或猜测自定义安装位置。

项目根按官方规则取最近含 `.git` 的祖先，否则是当前工作目录。直接下一层应当就是 `artifact-steward/SKILL.md`，不要多套一个压缩包名称目录；提供器不递归搜索任意深度的 SKILL.md。让宿主检查技能是否进入当前任务的目录并能读取引用资源，再运行用户指定目录的只读检查。

聊天与文件工具按当前会话实际能力接入；不将 DSH 内部事件日志直接当作 Codex 的 JSONL。导出的消息按 [聊天证据接入](chat-evidence.md) 归一化。技能不要求切换模型，也不更改账户、API 或客户端配置。

来源：[官方技能子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)、[官方桌面下载](https://deepseek.com/download/)。这些是文档支持的接法，不代表本包已在该客户端完成导入实测。

## Codex 与其他助手

Codex 可通过其当前支持的技能目录加载本文件夹。已有 `codex-artifact-steward` 时保留旧版，按名称明确选择，避免同一任务同时套用两套入口。只有宿主实际提供聊天读取工具时才使用；本包不包含 Codex 专用的本地会话解析器。

其他支持 SKILL.md 的代理，使用它们自己的技能导入入口或技能目录。若只有自定义提示词，把 SKILL.md 作为规则说明并保留可访问的 references；若无法加载引用文件，连同相关参考内容提供。普通 DeepSeek 聊天客户端、第三方套壳或网页不因使用 DeepSeek 模型就自动支持 Skills、终端或本地文件管理。

能力不足时完成可执行的只读工作并说明缺口。用户确需更强能力时，指出具体缺少文件访问、终端或历史导出中的哪一项；不要求无关密钥，不擅自安装软件或提升权限。
