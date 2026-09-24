# 项目阶段与执行结果

项目主循环 ProjectLoop 负责推进生成、审核、修复和执行。
术语、输入字段和人工审核规则见 [项目说明](README.md)。

## 启动与审核

普通启动和断点续跑：

```bash
/home/xinyu/.venv/bin/python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /home/xinyu/coral_npu.yaml
```

Stage 1 完成后返回 `AWAITING_SCENARIO_ROUTING`，等待验证负责人填写
`owner_review_path` 指向的表单。填写身份、每个场景的处理去向和必要意见后，
将下面的 `<job_id>` 替换为实际任务标识并提交：

```bash
/home/xinyu/.venv/bin/python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /home/xinyu/coral_npu.yaml \
  --scenario-routing "/home/xinyu/result/jobs/<job_id>/staging/validations/scenario_owner_review.json"
```

未提交完整审核表时，普通续跑仍会停在场景审核。
后续评审完成后自动执行，不要求最终人工批准或单独执行授权。

## 流程

1. 冻结项目配置、Spec、RTL、模型角色配置和 UVM 输入。
2. 场景生成：生成场景与验收条件映射，等待人工审核。
3. 用例规划：根据审核结果生成验收条件与用例映射。
4. 验证环境生成：UVM Worker 完成环境生成或修复，通过不含 RTL 的隔离 Xcelium 检查。
5. 测试代码生成：生成测试用例类，明确已实现和已跳过的用例，并进行初始评审。
6. 评审修复：存在可修复错误时，执行修复调度、结构校验、串行提交及最终评审；否则直接进入执行准备。
7. 自动执行准备：进入 `READY_FOR_EXECUTION_PREPARATION`，冻结测试代码、实际使用的 UVM、
   映射、评审、来源检查点、工具环境与资源约束，随后进入 `READY_FOR_BINDING`。
8. 执行绑定：绑定冻结 RTL 和 UVM 编译入口，进入 `READY_FOR_EXECUTION`。
9. 编译与仿真：完整编译和展开通过后，按清单逐个运行已实现用例。编译失败不运行，也不增加模型修复循环。
10. 发布结果：`EXECUTION_PASS`、`EXECUTION_FAIL` 或 `EXECUTION_BLOCKED`。

UVM 环境生成时的隔离编译不包含完整待测设计的功能仿真，不能替代第 9 步。
Spec 始终是预期行为、刺激和检查条件的唯一依据；RTL 在评审结束后才用于执行绑定和构建。
最终评审不要求没有任何问题，遗留错误、警告和覆盖缺口都会保留在执行结果中。

## 结果定位

任务结果根目录为 `/home/xinyu/result/jobs/<job_id>/`。
命令最终输出的 `execution_evidence_path` 指向执行证据文件，路径相对于该任务目录；
`summary`、`testcases`、`build` 同时给出汇总、逐用例结果和构建信息。

| 记录 | 定位方式 | 内容 |
|---|---|---|
| 评审完成记录 | 执行输入快照的 `source_checkpoint_path`；普通流程为 `audit/project_review_complete.json` | 评审完成及自动执行准备依据 |
| 执行输入快照 | `EXECUTION_INPUT_PATH` | 测试代码、UVM、用例清单、评审问题、工具环境和资源约束 |
| 执行源文件 | `execution/inputs/` | 生成用例、清单、UVM 文件和框架编译入口 |
| 执行绑定 | `EXECUTION_BUNDLE_PATH` | RTL、编译源文件、包含目录和顶层模块 |
| 执行请求 | `EXECUTION_REQUEST_PATH` | 完整构建及逐用例运行参数 |
| 工具请求和日志 | `audit/xcelium/`、`runs/xcelium/` | 原生工具请求、执行证据、输出树与日志 |
| 执行证据 | 输出中的 `execution_evidence_path`，对应 `EXECUTION_EVIDENCE_PATH` | 构建、逐用例结果、原始评审问题、覆盖缺口和计数 |
| 执行检查点 | `EXECUTION_RESULT_PATH` | 最终执行状态及恢复依据 |

表中的 `EXECUTION_*_PATH` 是 [执行模块](application/project_execution.py) 定义的固定路径常量。
查找实际结果时使用命令返回的路径或这些常量，不按文件时间选版本。

## 恢复与完整性

恢复校验任务标识、来源、内容指纹、文件完整性、工具环境和资源约束。
完整且匹配的构建和逐用例证据可以复用；缺失或漂移的证据拒绝复用。

既有任务只能从代码明确支持的检查点恢复。框架先验证来源检查点及其引用结果，再追加新的执行记录；
恢复来源文件保留原文和指纹，新执行检查点优先。已到执行准备阶段且来源完整时，
恢复不重复调用生成、修复或评审模型。

## 结果含义

- `generation_complete`、`review_complete` 表示阶段是否完成，不代表完整验证通过。
- `implemented`、`skipped`、`executed`、`passed`、`failed`、`blocked` 分别记录用例数量。
- 每条执行结果保留用例标识、UVM 类、随机种子、超时、通过标记、UVM 错误及致命错误计数和日志。
- 跳过用例保留原因，不计为通过；零个可执行用例报告 `EXECUTION_BLOCKED`。
- `full_spec_coverage_complete` 要求没有跳过用例、没有场景规格问题，且评审将所有验收条件标为已覆盖。
- `full_verification_passed` 要求存在可执行用例、执行通过、规格覆盖完整，且评审没有遗留问题。
- `EXECUTION_PASS` 只意味着已实现用例执行通过。存在跳过、规格遗漏或评审问题时，不能据此声称完整验证通过。

Xcelium 先完成整体构建检查，随后逐用例调用 `run()`。
当前每次 `run()` 都会重新编译、展开和仿真，不复用整体构建产生的仿真数据库。
工具隔离与日志说明见 [EDA 工具说明](adapters/eda/README.md)。

## 验证

运行统一自测入口：

```bash
/home/xinyu/.venv/bin/python \
  /home/xinyu/dv/scripts/validate_project_job_workflow.py
```

自测使用模拟模型服务、人工审核输入和模拟 Xcelium，覆盖生成、场景审核、修复提交、
自动执行、失败与超时、零用例、部分跳过、输入及日志篡改，以及构建和逐用例之间的中断恢复。
