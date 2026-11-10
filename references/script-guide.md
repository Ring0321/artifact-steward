# 脚本入口与运行边界

Python 3.11+。除 PDF 正文/页数外均为标准库；PDF 需要当前环境已有的 `pymupdf` 或 `pypdf`，缺失时明确返回 dependency_missing，不自动安装。保留完整 scripts 目录，不拆分共用模块。路径参数用真实绝对路径，命令遵循当前项目的 RTK 等约定。

## 索引与全目录重复组

```text
python -B -X utf8 <skill>/scripts/steward_index.py scan --root <源根> --state <独立状态目录> --entries 20000 --seconds 45 --hash-mib 512
python -B -X utf8 <skill>/scripts/steward_index.py duplicates --root <源根> --state <状态目录> --output <新重复组.json>
```

重复执行 scan 续查当前遍历；metadata_complete 后用 `--refresh` 开始新一轮，保留身份未变的完整哈希缓存。默认先哈希同大小候选；`--hash-all` 可覆盖其他文件。哈希没有64MiB单文件上限，每个已记录摘要都覆盖整个文件；单个文件可超过本轮软体积/时间预算，不保存部分摘要。超慢/阻塞 I/O 仍不提供硬超时。

元数据以 SQLite 保存目录进度；恢复时重枚举当前目录前缀，目录时间变化会重查该目录。它不是文件系统原子快照，不是后台监听器。已完成目录之后发生的变化要刷新；目录 I/O 错误和排除项进入报告，不称为全量完成。崩溃遗留 scan.lock 时先确认无扫描进程并核对状态，再由宿主管理该工具自有锁，不强行接管未知锁。

缓存仅用于候选发现，不能代替执行前实时哈希。状态目录与源根互不包含；首次必须为空，不接管任意非空目录，不读写客户端数据库。运行中索引、报告、保留记录不随技能分发。

## 跨位置副本的逐项判断

```text
python -B -X utf8 <skill>/scripts/steward_assess.py --review <逐项复核输入.json> --output <源根外的新判断报告.json>
```

输入见 [逐项复核示例](../assets/assessment.example.json)。root是本批允许处理的根；keepers可在另一个只读位置。角色由宿主根据真实用途填写，不能由文件名自动决定。保留原件、正式交付、项目依赖和独有历史；仅当role为redundant_copy、有稳定完整保留件或经完整读取验证的ZIP成员、声明的文本引用检查没有缺口时，给出propose_cleanup。应用缓存不能充当唯一持久保留件。

搜索覆盖指定reference_roots内的UTF-8文本，包含绝对路径和文件名，默认单文件4MiB、总32MiB。超预算、读错或引用命中均阻止直接建议清理。它不证明二进制或动态引用不存在；必要时宿主补查。批次不能互为唯一保留件。把required_protected_files中的路径带入精确执行计划，建议JSON始终不授予执行权限。

ZIP使用唯一、无歧义成员完整读取到EOF，核对CRC与SHA256，不解压文件。大ZIP会重复读取以核对归档自身的稳定性；这不是硬时限任务。不同内容的旧稿、图片PPT覆盖关系和语义替代关系由宿主按 [逐项判断](judgment.md) 补充实物依据。

## 文档与最后版本

```text
python -B -X utf8 <skill>/scripts/inspect_documents.py --file <文件> --output <新检查.json>
python -B -X utf8 <skill>/scripts/steward_review.py versions --root <源根> --state <状态目录> --evidence <聊天证据.json> --config <分组配置.json> --output <新版本报告.json>
```

无历史时省略 evidence。配置见 [配置示例](../assets/config.example.json)，按真实任务填写 version_groups，不以共同前缀合并独立用途。自动分组仅在同目录内推测同名家族；它不会覆盖所有跨目录迭代，需要宿主结合索引/聊天指定组。默认最多检查100个成员，超出路径列入 omitted_files；可用 `--max-files` 显式调整。

版本组可填 `requirement_checks`：键为实际绝对路径，值含 `status: met/unmet/unknown`、`evidence_refs` 和检查 `method`。引用形如 `task_id/turn_id/item_id`，须能在提供的真实消息中找到。此字段是宿主根据内容作出的有依据标注，不是自动语义理解，更不产生删除许可。最近交付仅是候选；原交付缺失时不降级冒认较早版本为最后稿。

DOCX 检查 XML 正文、表格数、插入/删除/移动修订数、批注/页眉页脚/脚注尾注与媒体字节摘要；不接受或拒绝修订、不验证排版。PPT 按 presentation.xml 的引用顺序核对实际页和备注，未引用的 slide/media 不当作页数。XLSX 读存储值和公式文本，不计算公式或重存。PDF 可提取文字与实际页数；不做 OCR，不证明页面视觉等价。包内 XML/媒体检查有64MiB总预算、单成员32MiB上限，超出会标为部分；页数/图片/正文相同也不构成完整文件相同。

原理参考：[Microsoft Open XML 演示文档](https://learn.microsoft.com/en-us/office/open-xml/presentation/how-to-get-the-titles-of-all-the-slides-in-a-presentation)、[PyMuPDF Document](https://pymupdf.readthedocs.io/en/latest/document.html)。

## 保留记录与分类

```text
python -B -X utf8 <skill>/scripts/steward_index.py remember --root <源根> --state <状态目录> --file <文件> --action keep --reason <保留理由> --evidence-ref <真实用户消息引用>
python -B -X utf8 <skill>/scripts/steward_index.py decisions --root <源根> --state <状态目录> --output <新保留记录.json>
python -B -X utf8 <skill>/scripts/steward_review.py classify --root <源根> --state <状态目录> --config <配置.json> --output <新分类提案.json>
python -B -X utf8 <skill>/scripts/steward_review.py move-plan --root <源根> --state <状态目录> --reviewed-moves <已审阅移动项.json> --output <新计划.json>
```

动作支持 keep/accept/defer/retire，必须是用户真实意图；没有自动到期清理。相同路径内容变化后不沿用旧“认可”，但保留/稍后复核仍按保守保护处理。记录按当前路径绑定，改名跨目录后宿主核对身份/完整哈希再登记关联，不自动把所有同内容副本视为用户已保留。

分类规则按顺序匹配，支持项目相对 prefix、glob、扩展名，输出 category/project。模板中的规则只是示例，不直接迁移用户项目。默认扩展名分类只是候选；不会自动判定源稿/最终稿等角色。冲突必须处理，所有已有链接和业务依赖由宿主复核。move-plan 只读取已审阅的明确移动项数组 `[{"path":"绝对路径","destination":"绝对路径"}]`，并将有效保留路径加入保护集。执行和恢复见 [精确执行](execution.md)。
