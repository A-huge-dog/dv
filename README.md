# Spec-only staged Project Job

`dv/` 当前只保留一条 Project Job vertical slice：

```text
one Project YAML
  -> immutable submission/Spec/RTL baseline
  -> configured Generator Spec-only Scenario↔AC map
  -> configured Generator Spec-only AC↔testcase/stimulus/check map
  -> configured Generator Spec-only portable SV testcase
  -> configured Reviewer Spec-only bundle review
  -> repairable ERROR: global FIFO + Orchestrator read-tool session
  -> Framework formalizes semantic repair-plan candidate and computes fingerprint
  -> deterministic Router receipt + scope-rich formal dispatch
  -> dispatched Stage Agent read-tool session + scoped replacement
  -> deterministic replacement validation
  -> SCOPED_REPLACEMENT_VALIDATED
  -> exact assembled-candidate Verilator precommit build
  -> canonical repair groups: just-in-time dispatch + atomic serial commit
  -> deterministic roots/dependency/impact recomputation after each commit
  -> one final Spec-only Reviewer call
  -> AWAITING_HUMAN_REVIEW
  -> explicit DV_OWNER testcase decision
  -> AWAITING_EXECUTION_AUTHORIZATION
  -> separate explicit DV_OWNER execution authorization
  -> exact approved testcase + immutable baseline RTL binding
  -> trusted Xcelium build, then run only after build PASS
  -> EXECUTION_PASS | EXECUTION_FAIL | EXECUTION_BLOCKED
```

三阶段 generation/review contract、全局 FIFO、真实 Orchestrator/Stage 工具会话、deterministic Router 和
replacement validation 和 OCHES003 commit runtime 已接入同一个 one-YAML CLI。OCHES002 只保存经过验证但尚未
提交的 replacement；OCHES003 按 canonical group（规范修复组）基于 exact current roots 即时 dispatch，先对精确
组装后的新 testcase 做 Verilator build-only 编译，只有整组 PASS 才以编号化 commit manifest 原子切换 current
authority。每次成功提交后都会重算 roots、dependency closure 和 impact；全部 groups 到达终态后只调用一次 final
Reviewer，并生成 Human review request/checkpoint 后进入 `AWAITING_HUMAN_REVIEW`。PJ-003 由唯一 ProjectLoop
继续处理 Human decision、独立 execution authorization、binding 和 trusted execution；PJ-004 才拥有最终 report
closure 和 `COMPLETE`。

## Authority

- Spec 是 expected behavior、scenario、AC、stimulus 和 oracle 的唯一 authority。
- Generator 三阶段与 Reviewer 不得读取或接收 RTL bytes/path/fingerprint/RTLIR/interface evidence。
- Spec 信息不足时返回 `SPEC_AMBIGUITY` / `BLOCKED_INPUT`，不得从 RTL 补全。
- RTL 只在 Human 批准 exact testcase 后进入 trusted binding/build/run。
- Mapping/testcase/review 都是 staging candidates；LLM 不能 approval 或 promotion。
- testcase approval 与 execution authorization 是两次不同的 `DV_OWNER` 外部提交，任何一份都不能替代另一份。
- `CheckpointRepository` 只读取 contract-owned 固定路径，不按目录或时间选择“最新” authority。

## PJ-003 approval and execution

testcase decision 与 execution authorization 必须在两次命令中提交：

```bash
python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /path/to/project.yaml \
  --decision /path/to/testcase_decision.json

python /home/xinyu/dv/scripts/run_project_job.py \
  --project-input /path/to/project.yaml \
  --execution-authorization /path/to/execution_authorization.json
```

第一条命令最多进入 `AWAITING_EXECUTION_AUTHORIZATION`；第二条命令验证 exact approved bundle、授权有效期、
opaque executable reference（不暴露实际命令路径的执行程序引用）和 environment fingerprint（环境指纹）后，自动完成
binding、build/run 和 immutable evidence 发布。restart 直接验证已登记 request/evidence 与完整 output tree，不重复
Provider、Human transition、build 或 run。

## Public input

Public `project_job_submission` schema `2.0` 只包含：

- Job/project identity；
- Spec/RTL workspace-relative paths；
- DUT top/parameters；
- 一个 code-owned Agent profile 路径；该 profile 再分别映射 initial Stage 1/2/3、repair
  Orchestrator/Stage 1/2/3、initial/final Reviewer 的 Provider config；
- EDA profile/timeout；
- Human `SPEC_OWNER` + `DESIGN_OWNER` input authority。

它不接受 fingerprint、source content、testcase identity、behavior/scenario/AC/testcase mapping sidecar、
credential value、testcase approval、waiver 或 disposition。

Framework 自动保存 exact submission、Spec、RTL、Agent profile 和全部被引用 Provider YAML bytes，
生成 internal manifest、完整角色 binding、source/input/authority fingerprints、testcase top 和 unique
pass marker。Derived mappings 只写 `staging/`，不得回写 `input_baseline/`。完整 Project Job 的 CLI 仍只接受
一个总 YAML；模型不能通过额外 CLI 参数或 sidecar 配置传入。

## Main files

- `application/`：REF-004 的 application handlers（应用处理器）。每个 handler 只执行一次明确业务动作，显式接收
  exact artifact、lineage/authority 和 Provider/persistence/EDA dependency，并返回带精确输出路径的 typed result。
  这里分别包含 bootstrap、Stage 1/2/3 generation、initial/final review、repair plan、scoped replacement、
  compile candidate、commit group、impact recomputation 与 Human gate；handler 不搜索“最新 Job/revision/artifact”。
- `runtime/project_loop.py`：唯一 Project loop（项目循环）。它从固定 authority path 读取 checkpoint，按显式
  transition table 推进 AUTO 状态，并在 Human/operator/retry/terminal 边界暂停；不实现 Agent 工具协议。
- `runtime/project_job.py`：输入校验、Provider budget/probe 与 bootstrap 支撑；bootstrap 的持久化动作由
  `application/bootstrap.py` 单独拥有。
- `agents/profile.py`：从总 YAML 递归解析、校验并绑定统一 Agent profile 与 Provider config
  快照；运行时按角色返回 exact provider/model lineage。
- `runtime/staged_workflow.py`：三阶段 workflow 协调、no-RTL inspection、checkpoint 与 Provider session 支撑；
  generation/review/Human gate 动作已调用 `application/` 中的独立 handler。production Stage 3 先做确定性 candidate-contract
  validation，再用 Verilator 检查 exact assembled bytes，Generator candidate 不携带 AC-level code evidence；
  只在 Router formal dispatch 后执行一次回流。
- `domain/stage1.py`、`stage2.py`、`stage3.py`：纯确定性的三阶段 candidate 生成、map/traceability/
  testcase validation 与 Stage 3 assembly/enrichment；不读取 Job 文件，不调用 Provider，也不推进 checkpoint。
- `domain/review.py`：Reviewer request/report/validation 与 bounded diagnostic 规则。
- `domain/artifacts.py`：artifact/direct-lineage fingerprint、semantic unit、index、assembly 和 commit-candidate
  确定性规则；`infrastructure/persistence/artifact_store.py` 只负责这些 artifact 的显式路径读写。
- `domain/repair.py`：repair plan 形式化、Router authority 校验、scoped replacement、canonical grouping 与 impact
  计算；Router 不做语义路由，domain 层不拥有 workflow 或 commit authority。
- `application/scoped_repair.py`：显式 authority 文件与 transcript lineage 的边界校验；业务 replacement
  规则位于 `domain/repair.py`。
- `infrastructure/persistence/repair_records.py`：统一 Provider stop mapping、五角色 versioned prompt contract、十类 append-only
  repair records、四类派生索引及 serial repair executor。
- `runtime/job_runtime.py` 与 `runtime/repair_runtime.py`：one-YAML Project loop 使用的 FIFO/repair 协调层；从每个 Job immutable
  manifest 的 Provider snapshot 创建对应角色，按 checkpoint 恢复并停止在未提交的 validated replacement；
  对只有 request、没有 response/plan/replacement 的 terminal `PROVIDER_UNAVAILABLE`，校验完整 append-only
  evidence 后最多追加一次新 attempt，并切换到新的 session ID。
- `runtime/commit_runtime.py`：OCHES003 串行协调层；compile、group commit、impact、final Reviewer 与 Human
  gate 分别调用独立 application handler。每个编号化 commit manifest 是对应 group 的唯一 authority switch，失败组
  不改变 current roots，已成功的先前 group 不回滚。
- `runtime/agent_loop.py`：唯一的单 Agent 协议循环；既支持原有的一次性提交协议，也支持 UVM Worker 的
  observation/action/terminal 连续协议。连续 Worker 在同一 session 中反复修改和验证，只有 Framework 的
  CompletionValidator（完成校验器）通过后才成功；budget、恢复和 exact replay 也由同一实现控制。
- `infrastructure/persistence/transcript_store.py`：append-only transcript（只追加对话记录）的唯一文件持久化实现；
  它只负责事件、manifest、序号和指纹的安全写入与读取，不决定工具权限、业务流程、commit 或 Human authority。
- `agents/project_tools.py`：从一个经过 fingerprint/lineage 校验的 exact current Job snapshot 提供 9 个只读工具；
  复用现有 unit/index、Reviewer report、Spec baseline 和 repair records，不建立第二套 evidence authority；accepted
  formal plan 可成为 authority，rejected plan 仅在 exact receipt + transcript 配对后作为 untrusted failure history。
- `runtime/standalone_stage3.py`、`runtime/standalone_reviewer.py` 与对应 CLI：从 immutable
  source Job 运行隔离的 test-only Stage 3；输出不得 promotion、approval 或进入 EDA。
- `adapters/llm/`：configured OpenAI-compatible provider boundary；model identity 由 config 选择。
- `adapters/eda/`：trusted no-shell EDA boundary。Xcelium adapter 提供隔离的 version probe、
  compile/elaboration 和完整 run。UVM Worker 在 Human gate 前只通过固定 Framework harness 做 UVM
  compile/elaboration，不读取 RTL；Human 批准后的 Project execution 仍使用独立 authorization 和 binding。使用边界见
  [`adapters/eda/README.md`](adapters/eda/README.md)。
- `contracts/project/`：Project submission、internal manifest、mapping、candidate、unit/index/assembly、
  review、impact 和 report contracts。
- `tests/agent_runtime/`：current Project positive/negative tests。

## Development validation

```bash
/home/xinyu/.venv/bin/python \
  /home/xinyu/dv/scripts/validate_project_job_workflow.py
```

Self-test must use scripted/mock providers、scripted Human submissions and fake/test-only EDA, and prove no production
Generator/Reviewer、Human decision、legacy/new production Job or production Project EDA evidence occurred.

Self-test 只调用 scripted providers 和 fake compile runner。无可修复 ERROR 时进入
`AWAITING_HUMAN_REVIEW`；有可修复 ERROR 时先由 OCHES002 产生 `SCOPED_REPLACEMENT_VALIDATED`，再由 OCHES003
按 canonical groups 执行 build-only 编译、serial commit、impact 和 final review。失败 group 记录
`NOT_COMMITTED` 且不切换 current authority；全部 groups 到达终态后，无论 final report 为 CLEAN、ERROR 或 WARNING，
都只生成 Human review request/checkpoint 并进入 `AWAITING_HUMAN_REVIEW`。测试不执行 production EDA、真实 Human
decision 或 promotion。PJ-003 fake Xcelium qualification covers approval/authorization、binding、PASS/FAIL/BLOCKED、
restart/replay、partial/tamper/cross-Job and complete output-tree verification.

## Standalone Stage 3 test

当 source Job 已有有效 Stage 1/2、但没有有效 Stage 3 candidate 时，可只重测 Stage 3：

```bash
python /home/xinyu/dv/scripts/run_project_stage3.py \
  --stage3-input /home/xinyu/stage3_R.yaml
```

YAML 是唯一运行输入，选择独立 Stage 3 Job ID、source Project Job 中 exact Stage 1/2 mapping、Stage 3
Generator config 和 Human `STAGE3_TEST_OWNER` authority。修改 Stage 3 模型时创建新的 provider config，
在新 YAML 中引用它并使用新的 Stage 3 Job ID；source Stage 1/2 不重跑。

该命令只进行一次 Provider 调用；无效响应保存 rejection evidence 后失败关闭，不自动 repair。所有新 evidence 只写 YAML 指定的
`result/jobs/<stage3_job_id>/`；source Job 保持只读。该目录是明确的 test-only Job，不是完整 Project
Job，不得提交 Human approval、promotion 或 EDA execution。

## Legacy

`JOB.PROJECT.AXI_LITE_SRAM.FIRST_RUN.R1`、preauthored mapping sidecars、旧的分散 Provider 字段、
fingerprint-bearing Project input 和 retired Phase A～G/R artifacts 都只属于历史 evidence。它们不能被
retry、approved、promoted、executed or migrated into a current Job。
