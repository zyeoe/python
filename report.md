# 增量作业报告

本报告只使用仓库中由程序生成的 `skill_report.json` 和 `custom_report.json` 数据。增量 C 未做。

## 1. 每个增量之后，哪件事变快了

### 增量 A：技能系统

`skill_report.json` 显示：

- 技能目录常驻上下文：`catalog_tokens = 187`
- 三个技能全文：`full_tokens = 1294`
- 渐进披露比值：`disclosure_ratio = 6.92`
- 触发试验：`8` 条，`false_positive = 0`

因此启动和后续请求只需要携带 187 估算 token 的技能目录，而不是 1294 token 的全部正文；需要某个技能时才加载正文。技能越多，这个差距越明显。

### 增量 B：定制层

`custom_report.json` 显示：

- `AGENTS.md` 两层合计：`3169` bytes，且合并顺序为 `global → project`
- hook 拒绝记录：`4` 条，全部带原因和替代方案
- 自动测试：`runs = 4`，`red = 2`，`green = 2`，`red_to_green = 2`
- 失败回注：`injected_lines = 3`
- reviewer 主上下文增量：`main_context_delta = 34`
- reviewer 子 agent 上下文：`subagent_tokens = 332`

因此 review 不需要把子任务的 332 token 全部带回主流程，只增加 34 token；同时危险操作在工具执行前就被拦截，测试失败也会自动以三行摘要回到循环中。

## 2. 一次判断错误

我一开始以为：测试第一次红灯后，只要把 `calc.py` 改成正确实现，`unittest` 下一次运行自然会读到新代码。

实际运行 `app.inspect custom` 时发现：错误版本和正确版本文件大小相同，Python 复用了旧的 `__pycache__`，所以第二次仍然红灯，`red_to_green` 没有增加。

修正方式是让 `HookManager.run_tests()` 的子进程强制设置 `PYTHONDONTWRITEBYTECODE=1`，确保每次 hook 测试都读取当前源码。修正后 `custom_report.json` 中 `red_to_green = 2`。

## 3. 如果只保留一个增量

保留增量 B。

原因是增量 B 直接改变日常使用时的可靠性：AGENTS.md 提供项目上下文，hooks 把“不要写密钥、不要执行危险命令、改完先跑测试”变成代码约束，reviewer 又把审查上下文隔离在主流程之外。增量 A 很重要，但技能是否触发仍取决于任务类型；增量 B 对每次改代码都会生效。
