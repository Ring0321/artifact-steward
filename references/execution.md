# 精确执行、可恢复处理与核验

`execute_confirmed_file_batch.py` 默认预览。它不判断业务价值，也不验证批准引用的真实性；宿主必须核实当前可信会话中用户确实批准了展示的具体批次。导入计划、旧聊天和 JSON 标记都没有授权效力。

## 生成计划和预览

```text
python -B -X utf8 <skill>/scripts/execute_confirmed_file_batch.py --mode plan --root <源根> --items <具体文件数组.json> --protected <保留文件> --operation quarantine --recovery-root <同卷独立恢复区> --result <新计划.json>
python -B -X utf8 <skill>/scripts/execute_confirmed_file_batch.py --plan <计划.json> --expected-plan-sha <该计划SHA256> --result <新预览.json>
```

items 是 `[{"path":"物理绝对路径","id":"A01"}]`；移动项另含 destination。`--protected` 可重复。每个目标绑定卷/文件ID、大小、纳秒修改时间、链接数和完整SHA256；根目录绑定身份。grants_execution 始终为 false。分类移动优先用 move-plan 自动引入保留记录；其他计划由宿主将当前保留记录和必要配套加入 --protected，不漏掉跨任务引用。

支持 quarantine（进入恢复区）、move（源根内分类移动）、permanent_delete（永久删除）。计划生成默认 quarantine，导入的计划必须明确包含operation，缺少时拒绝执行。不能把旧永久删除许可换成其他范围；用户选择的操作方式必须展示清楚。预览不处理源文件，也不产生执行许可。

## 确认后显式执行

```text
python -B -X utf8 <skill>/scripts/execute_confirmed_file_batch.py --mode apply --plan <计划.json> --expected-plan-sha <已展示计划SHA256> --approval-ref <当前真实用户确认引用> --result <新执行结果.json>
```

`--id` 可重复指定已批准子集；同批正常分块使用同一计划、各块独立结果，不能更新身份来让旧计划通过。未选项仍在时保护其内容，先前已不存在的未选项单列，不计本批删除。已不存在的选中项也单列，不能算作本次回收。

开始前强制检查计划摘要（优化模式也不能跳过）、根身份、删除/移动与保护集按路径和文件身份不重叠、短ID和目标路径唯一、目标不覆盖其他源、链接/占位拒绝、保留件哈希与现场源身份/内容。变化、目标冲突或占用会拒绝/跳过并记录；不杀进程。

普通执行器还拒绝已知应用托管、配置和数据库路径；判断报告的required_protected_files必须进入保护集。扩展应用或项目保护范围时可在计划中登记protected_roots；宿主仍须核对实际引用和当前可信会话的授权。

恢复区与源根、报告目录分离。当前可恢复移动仅支持同卷，Windows 使用不覆盖的原生重命名，保留文件对象及其流；POSIX 用排他硬链接后移除旧名称，临时双名称由日志记录。跨卷会拒绝，需另走验证内容及元数据的复制流程；不能静默丢掉备用数据流或安全信息。恢复区不自动到期删除，同卷恢复区仍占磁盘。

执行结果和逐项 JSONL 日志放源根/恢复区外，保留 intent、移动/删除结果和结束状态。最终结果只在批次结束重写，日志逐项落盘，避免每个文件重写整个大型 JSON。中断保留已有记录，不覆盖盲跑。

## 恢复与中断核对

```text
python -B -X utf8 <skill>/scripts/execute_confirmed_file_batch.py --mode restore --restore-result <已核实执行结果.json> --expected-result-sha <结果SHA256> --approval-ref <真实恢复指令引用> --result <新恢复结果.json>
python -B -X utf8 <skill>/scripts/execute_confirmed_file_batch.py --mode reconcile --plan <原计划.json> --expected-plan-sha <计划SHA256> --restore-result <原执行结果.json> --result <新对账.json>
```

restore 只处理记录为 moved_verified 的文件，核对原根身份、恢复文件身份与完整哈希；原位置已有新文件则保留两者并报告冲突。不会恢复永久删除。中断尚无最终成功记录的项先 reconcile 结合逐项日志核对，报告不自动猜测成功、不自动重试或补删。POSIX 崩溃可能遗留双链接，须按日志核对再由宿主提出具体修复。

统计区分永久删除数、实际移动数、同根迁移量、源根逻辑移除量、恢复区占用和观察到的卷空间变化。保留件批前/批后完整复核，逐项检查其身份；这不是对抗恶意并发替换的句柄级隔离或整个批次的原子事务。数据库、Git对象、客户端记录及原生回收站不套用此入口。
