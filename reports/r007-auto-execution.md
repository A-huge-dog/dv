# 自动 testcase 执行改造与 R007 验收

日期：2026-09-10

改造完成，真实 R007 已使用原命令续跑。执行结果为 **EXECUTION_FAIL：完整构建失败**，没有运行 testcase，未声称验证通过。

## 改造结果

- 保留 Stage 1 场景人工审核；删除最终 testcase 批准、单独执行授权及对应命令参数。
- 初始评审无需修复时、既定修复与最终评审完成后，都自动进入执行准备。
- Framework 冻结来源检查点、candidate、映射、effective UVM、生成类、测试清单、评审、工具环境及资源约束，并绑定冻结 RTL。
- 复用既有 Xcelium 编译顺序、隔离执行与证据复用机制。Framework 测试包导入所选 UVM 包，并在正常完成 final phase 且 UVM 错误计数为零时输出清单中的通过标记。
- 完整构建 PASS 才逐用例执行；跳过、零用例、失败、超时与规格覆盖状态分别记录。
- 恢复优先读取新执行检查点，保留旧检查点原文；完成证据的输出树与输入指纹仍需校验。

## 真实 R007 结果

原命令：

```bash
/home/xinyu/.venv/bin/python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /home/xinyu/coral_npu.yaml
```

| 项目 | 结果 |
|---|---|
| Job | `JOB.PROJECT.CORAL_NPU.SPEC_AUTH.R007` |
| 来源检查点 | `CHECKPOINT.PROJECT.HUMAN.C466E01E86B0C91E` |
| 构建 | FAIL：Xcelium 26.03-e065 编译失败，未完成展开 |
| TC.0012 | BLOCKED：BUILD_NOT_PASSED；seed 598146335；timeout 120 秒 |
| TC.0025 | BLOCKED：BUILD_NOT_PASSED；seed 826088541；timeout 120 秒 |
| 实现 / 执行 / 通过 / 用例失败 / 阻塞 / 跳过 | 2 / 0 / 0 / 0 / 2 / 28 |
| 原始评审 | 4 ERROR、1 WARNING，全部保留 |
| 需求覆盖评审 | 28 OMITTED、2 BLOCKED，全部保留 |
| 完整规格覆盖 / 完整验证通过 | false / false |

首个编译错误出现在冻结的 `RvvCoreMiniAxi.sv:201483`：`VLEN` 宏未定义，随后出现类型与语法错误。
日志的文件级统计显示，所选 UVM 包、接口、top、生成测试包与 Framework top 的解析错误数均为 0。
本次未修改冻结 RTL 或补充推测的宏值，未启动额外模型修复循环。

正式证据：

- [CLI 结果](r007-auto-execution.json)
- [完整执行汇总](/home/xinyu/result/jobs/JOB.PROJECT.CORAL_NPU.SPEC_AUTH.R007/audit/pj003_execution_evidence.json)
- [Xcelium 构建日志](/home/xinyu/result/jobs/JOB.PROJECT.CORAL_NPU.SPEC_AUTH.R007/runs/xcelium/pj003.build.6f33baff3fa77a19/build/xrun.log)
- [冻结执行输入](/home/xinyu/result/jobs/JOB.PROJECT.CORAL_NPU.SPEC_AUTH.R007/audit/pj003_execution_input.json)
- [执行绑定](/home/xinyu/result/jobs/JOB.PROJECT.CORAL_NPU.SPEC_AUTH.R007/audit/pj003_execution_bundle.json)

## 验证证据

- 执行链相关回归：46/46 通过，包括 ProjectLoop、修复提交、Xcelium、结果汇总、零用例、ERROR findings、部分跳过、超时、篡改拒绝与中断恢复。
- 生成、评审、增量结果、EDA-003 与应用架构相关回归：65 项通过。
- UVM Worker 的 ProjectLoop 集成回归：8/8 通过；测试已更新为自动执行终态。
- Python 静态编译与 `git diff --check` 通过。
- 临时副本 `/tmp/r007-execution-t1daifrk` 验证了原检查点恢复、真实完整构建、编译失败不运行和重复启动证据复用；禁止创建模型 Provider 的检查未触发。
- 正式 R007 再次执行同一命令后结果完全相同。续跑前原有的 **823 个文件内容与修改时间全部保持不变**，包括原检查点、生成/修复/评审结果及 scheduler 状态。

构建失败属于此次真实执行发现的输入问题；自动执行改造与失败阻止运行的验收已完成。
