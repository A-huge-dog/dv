# 基于规格的芯片验证流程

本项目从一份 YAML 项目配置出发，根据 Spec（设计规格）生成验证场景、验收条件和测试用例，
经过人工场景审核、模型评审及必要的修复后，自动编译并运行已实现的用例。
每次项目任务称为 Project Job，输入、生成结果和执行记录都按任务保存。

运行前需要在终端输入api key：
```bash
export OPENAI_API_KEY=
```
并且启动Xcelium
```bash
export XCELIUM_HOME=/arm/tools/cadence/xcelium/26.03_e065
export PATH="$XCELIUM_HOME/bin:$XCELIUM_HOME/tools/bin/64bit:$XCELIUM_HOME/tools/bin:$PATH"
```

## 使用 ChatGPT 进行 scenario routing 的prompt
现在你作为dv owner，对这个文件进行commnet和routing的填充，你自行根据spec的source判断是进行comment补充还是宣告为spec的问题，对checkable的同样进行检查
comment 是自由文本字段， routing.destination 只能填写“AC_TESTCASE_MAP_AND_TESTCASE”、“SCENARIO_AC_MAPPER”、“SPEC_AGENT”这三个字段

## 主要流程

1. **冻结输入**：保存项目配置、规格、RTL、UVM 输入及模型配置的不可变快照。
2. **场景生成（Stage 1）**：生成场景与 AC（Acceptance Criteria，验收条件）的对应关系，然后等待人工场景审核。
3. **用例规划（Stage 2）**：根据审核结果生成验收条件与测试用例的对应关系，明确刺激和检查条件。
4. **验证环境生成**：UVM Worker（验证环境生成 Agent）生成或修复环境，通过不含 RTL 的隔离 Xcelium 编译和展开检查。
5. **测试代码生成（Stage 3）**：生成 SystemVerilog 用例，校验结构，并将每个可检查用例明确归为已实现或已跳过。
6. **评审与修复**：Reviewer（评审 Agent）根据规格评审用例。存在可修复错误时，执行修复调度、校验和提交，
   所有修复组结束后进入最终评审；无需修复时，初始评审完成后直接进入执行准备。
7. **自动执行**：进入 `READY_FOR_EXECUTION_PREPARATION`，冻结评审结果和实际使用的 UVM 环境，
   绑定 RTL，完整编译通过后逐个运行已实现用例，发布执行结果。

Stage 1 后的场景审核是流程中的人工提交点。后续评审完成后自动执行，不再要求最终人工批准或单独执行授权。
`EXECUTION_PASS` 只表示已实现用例执行通过，完整验证是否通过由 `full_verification_passed` 单独表达。

## 输入与证据边界

- 场景、验收条件、刺激和预期结果只从 Spec 推导。规格信息不足时报告 `SPEC_AMBIGUITY` 或 `BLOCKED_INPUT`。
- 三阶段生成 Agent 与 Reviewer 不得接收 RTL 的内容、路径、指纹或从 RTL 提取的接口证据。
  指纹是对内容计算的摘要，用于检测文件是否变化、核对结果是否属于同一份输入。
- RTL 在启动时冻结保存，评审流程结束后才绑定到执行输入并用于编译、运行。
- 映射、测试代码和评审报告先作为候选结果保存。模型不能自行批准结果或将其切换为项目正式使用的版本。
- 最终评审的错误、警告和覆盖缺口保留在结果中，不单独阻止执行。
- 恢复只读取代码规定的固定路径和正式引用，不按目录或修改时间寻找“最新结果”。
- 历史手写映射、旧输入格式和已停用任务的结果不能直接作为当前任务的输入或执行依据。


## 启动与场景审核

依赖版本记录在 [requirements.lock](requirements.lock)。已配置环境中的普通启动和断点续跑命令为：

```bash
/home/xinyu/.venv/bin/python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /home/xinyu/coral_npu.yaml
```

首次运行在 Stage 1 完成后返回 `AWAITING_SCENARIO_ROUTING`。
命令输出的 `owner_review_path` 指向任务目录内的审核表：

```text
/home/xinyu/result/jobs/<job_id>/staging/validations/scenario_owner_review.json
```

由验证负责人填写 `actor.identity`，并为每个场景填写 `routing.destination`。
`actor.role` 保持 `DV_OWNER`（验证负责人）。每个场景必须恰好出现一次，去向可选：

| `routing.destination` | 处理方式 | 填写要求 |
|---|---|---|
| `AC_TESTCASE_MAP_AND_TESTCASE` | 进入用例规划与生成 | 场景状态必须为 `CHECKABLE` |
| `SCENARIO_AC_MAPPER` | 返回场景与验收条件生成环节修正 | `comment` 必须写明修改意见 |
| `SPEC_AGENT` | 记录为规格问题 | `comment` 必须写明问题 |

只填写身份、意见和去向，保留场景内容、规格引用、任务标识和指纹。
将下面的 `<job_id>` 替换为实际任务标识，提交完整审核表并继续：

```bash
/home/xinyu/.venv/bin/python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /home/xinyu/coral_npu.yaml \
  --scenario-routing "/home/xinyu/result/jobs/<job_id>/staging/validations/scenario_owner_review.json"
```

未提交完整表单时，普通续跑仍停在场景审核。提交后，框架保存不可变审核记录；
原审核记录不能修改，后续普通续跑直接复用该记录。
已生成阶段结果、但评审尚未完成的受阻任务，也可通过 `--retry-blocked-review` 显式重试评审；
该参数不能与 `--scenario-routing` 同时使用。

## 自动执行与结果

评审流程结束后，框架冻结候选测试代码、实际使用的 UVM 环境、映射、评审记录与工具配置，
再绑定不可变的 RTL 基线和编译入口。工具位置、资源上限来自
[environment/xcelium.json](environment/xcelium.json)，工具配置与超时来自冻结的任务配置；
私有环境变量不写入结果。

完整编译通过后，按清单中的用例名称、随机种子、超时和通过标记逐个执行。
框架为每个生成测试类注册 `dv_exec_<uvm_class>` 子类，并通过
`+UVM_TESTNAME=dv_exec_<uvm_class>` 选择它。UVM（通用验证方法学）规定此命令行参数
优先于 `run_test("directed_test")` 中的默认名称，因此顶层指定默认测试并不妨碍生成用例执行。
生成测试类需要支持标准的 `(name, parent)` 构造参数，并在 UVM 阶段中运行对应的激励和检查；
继承默认测试的行为并不等于实现了映射用例。生成、评审和修复环节共用这份执行约定。
编译失败时不运行用例，也不自动增加模型修复循环。跳过用例保留原因，零个可执行用例报告阻塞。

| 状态或字段 | 含义 |
|---|---|
| `EXECUTION_PASS` | 已实现用例全部执行通过 |
| `EXECUTION_FAIL` | 构建失败或至少一个用例执行失败 |
| `EXECUTION_BLOCKED` | 在没有执行失败的情况下，没有可执行用例或存在阻塞 |
| `generation_complete`、`review_complete` | 生成和评审阶段是否完成 |
| `full_spec_coverage_complete` | 没有跳过用例、场景规格问题，且评审将所有验收条件标为已覆盖 |
| `full_verification_passed` | 存在可执行用例、执行通过、规格覆盖完整，且评审没有遗留问题 |

结果保存在 `/home/xinyu/result/jobs/<job_id>/`。命令输出包含 `summary`、`testcases`、
`build` 和 `execution_evidence_path`；最后一个字段给出执行证据文件相对于任务目录的路径。
证据保留构建及逐用例日志、错误计数、原始评审问题、覆盖缺口，以及实现、跳过、执行、通过、失败和阻塞数量。

恢复会校验输入、内容指纹和完整输出树，复用已完成且匹配的构建与用例证据。
既有任务只允许从代码明确支持且校验通过的检查点恢复，恢复来源文件保持原文，新执行检查点优先。
结果文件及定位方式详见 [阶段与执行结果](PROJECT_JOB_STAGE_SUMMARY.md)。

## 评审修复

可修复错误进入全局 FIFO（First In, First Out，先进先出）队列。
修复编排 Agent 通过只读工具查看规格、评审和当前结果，提出包含修复范围的计划。
框架校验计划并计算指纹，Router（分发器）按计划确定接收任务的阶段 Agent，不自行做语义判断。

阶段 Agent 读取被分配的证据并生成限定范围内的替换结果。替换结果通过校验后，先保存为待提交候选。
提交协调器按规范修复组串行推进：根据当前版本即时分配任务，检查修复后的 UVM 环境，
生成并校验测试代码结构，然后通过提交清单原子切换正式版本。
这里的原子切换表示一个修复组要么完整生效，要么不改变当前版本；已成功提交的先前修复组不回滚。

每次成功提交后，重新计算结果根指纹、依赖关系和影响范围。
所有修复组到达终态后，进入一次最终评审阶段；若评审输出不符合要求，则按修正预算处理。
测试代码的完整编译和运行统一在评审结束后执行。

## 主要模块

| 目录或文件 | 职责 |
|---|---|
| `application/` | 应用处理器：每个处理器执行一次明确业务动作，接收具体输入与依赖，返回输出路径 |
| `runtime/project_loop.py` | 项目主循环：按检查点推进自动状态，在人工输入、预算、重试或终态边界暂停 |
| `runtime/project_job.py`、`application/bootstrap.py` | 输入校验、模型预算与服务能力检查，以及输入快照保存 |
| `agents/profile.py` | 解析角色配置、保存模型配置绑定，并核对使用的模型来源 |
| `runtime/staged_workflow.py` | 协调三阶段生成、场景审核、UVM 生成和评审，确保模型请求不包含 RTL |
| `domain/stage1.py`、`stage2.py`、`stage3.py` | 确定性校验、映射构造、可追溯关系和测试代码组装；不调用模型或推进流程 |
| `domain/review.py` | 构造与校验评审请求、报告及诊断信息 |
| `domain/artifacts.py`、`infrastructure/persistence/artifact_store.py` | 结果指纹、依赖关系、索引、组装规则及固定路径读写 |
| `domain/repair.py`、`application/scoped_repair.py` | 修复计划、分发校验、限定范围替换、修复分组与影响计算 |
| `runtime/job_runtime.py`、`runtime/repair_runtime.py` | 全局队列与修复会话协调，保存待提交且已通过校验的替换结果 |
| `runtime/commit_runtime.py` | 串行提交修复组，重算影响范围并完成最终评审 |
| `application/project_execution.py` | 冻结执行输入、绑定 RTL、运行已实现用例并发布结果 |
| `runtime/agent_loop.py` | 单 Agent 协议循环：管理读取、提交和连续修改工具调用，以及预算与恢复 |
| `infrastructure/persistence/transcript_store.py`、`repair_records.py` | 持久化只追加的对话及修复记录；已有记录不覆盖 |
| `agents/project_tools.py` | 从校验过的当前任务快照提供只读证据工具 |
| `runtime/standalone_stage3.py`、`runtime/standalone_reviewer.py` | 从来源任务运行隔离的测试代码生成与评审 |
| `adapters/llm/`、`adapters/eda/` | 连接模型服务和仿真工具，隔离外部调用 |
| `contracts/`、`tests/agent_runtime/` | 数据格式约束与自动测试 |

Stage 3 只检查生成结果是否满足结构和映射要求，允许全部跳过的候选进入评审。
程序不通过跳过理由中的关键词或已有信号、函数名猜测实现能力；
跳过是否合理、覆盖是否完整由后续语义评审判断，生成阶段校验通过不代表验证完成。

连续运行的 UVM Worker 会反复修改和验证，只有框架的 CompletionValidator（完成校验器）
确认结果满足要求后才成功。UVM 环境检查在场景人工审核之后、测试代码生成之前进行；
完整 RTL 仿真在评审之后自动执行。工具边界见 [EDA 工具说明](adapters/eda/README.md)。

## 开发验证

统一自测入口：

```bash
/home/xinyu/.venv/bin/python \
  /home/xinyu/dv/scripts/validate_project_job_workflow.py
```

自测使用模拟模型服务、预先编写的场景审核输入和模拟 Xcelium，不调用生产模型或真实仿真工具，
也不修改生产任务结果。测试覆盖生成、场景审核、评审修复、自动执行及恢复；
自动执行回归包含编译失败不运行、逐用例失败、超时、跳过和零用例、输入及日志篡改拒绝，
以及构建和逐用例之间的中断恢复。

## Agent 执行预算

共享默认预算定义在 [domain/budgets.py](domain/budgets.py)，用于三阶段生成、UVM Worker、
Reviewer 和修复编排 Agent 的循环：

- 模型轮次最多 60 次；每轮最多调用一个工具，读取也消耗模型轮次。
- UVM Worker 最多执行 30 次动作；写入和编译共用额度，读取不消耗动作额度。
- 读取工具会话最多读取 24 次，同一工具可重复读取不同证据。
- 每次受预算控制的运行或会话最多 3600 秒；token（模型计量文本的单位）总量上限为 4,000,000。
- 初始三阶段候选失败后，每次进入修正流程最多新增 3 次修正。
- 初始或最终评审每次进入处理器最多提交 4 次，即首次评审加 3 次修正。

修正读取已保存的失败记录并反馈最新诊断；仍未通过则保存记录并暂停，同一任务可续跑。
各层上限共同生效，外层调用、时间或 token 预算可能先耗尽。
模型服务的网络重试与候选修正分别计数；人工暂停发生在场景审核环节。
