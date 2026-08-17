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
```

三阶段 generation/review contract、全局 FIFO、真实 Orchestrator/Stage 工具会话、deterministic Router 和
replacement validation 和 OCHES003 commit runtime 已接入同一个 one-YAML CLI。OCHES002 只保存经过验证但尚未
提交的 replacement；OCHES003 按 canonical group（规范修复组）基于 exact current roots 即时 dispatch，先对精确
组装后的新 testcase 做 Verilator build-only 编译，只有整组 PASS 才以编号化 commit manifest 原子切换 current
authority。每次成功提交后都会重算 roots、dependency closure 和 impact；全部 groups 到达终态后只调用一次 final
Reviewer，并生成 Human review request/checkpoint 后进入 `AWAITING_HUMAN_REVIEW`。它不执行 simulation、真实
Human decision、promotion 或 DUT functional qualification。

## Authority

- Spec 是 expected behavior、scenario、AC、stimulus 和 oracle 的唯一 authority。
- Generator 三阶段与 Reviewer 不得读取或接收 RTL bytes/path/fingerprint/RTLIR/interface evidence。
- Spec 信息不足时返回 `SPEC_AMBIGUITY` / `BLOCKED_INPUT`，不得从 RTL 补全。
- RTL 只在 Human 批准 exact testcase 后进入 trusted binding/build/run。
- Mapping/testcase/review 都是 staging candidates；LLM 不能 approval 或 promotion。

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

- `core/project_job.py`：one-YAML bootstrap 与公共 staged workflow 入口。
- `core/project_agent_profile.py`：从总 YAML 递归解析、校验并绑定统一 Agent profile 与 Provider config
  快照；运行时按角色返回 exact provider/model lineage。
- `core/project_staged.py`：三阶段 generation、no-RTL inspection、mapping/testcase/review validation、
  sharding、checkpoint、per-unit 持久化与 Human review gate；production Stage 3 先做确定性 candidate-contract
  validation，再用 Verilator 检查 exact assembled bytes，Generator candidate 不携带 AC-level code evidence；
  只在 Router formal dispatch 后执行一次回流。
- `core/project_repair.py`：把 Orchestrator 只含 `status + repairs` 的语义 candidate 形式化为绑定 exact
  Job/session/root/profile/config/provider/request/response 的 repair plan，并由 Framework 计算 fingerprint；随后校验
  current-Job/scope/roots/lineage/target，签发或拒绝 formal dispatch。Router 不做语义路由。
- `core/project_oches003.py`：确定性 canonical grouping、统一 Provider stop mapping、五角色 versioned prompt
  contract、十类 append-only repair records、四类派生索引及 serial repair executor。
- `core/project_job_runtime.py`：公开 one-YAML 入口与 FIFO/repair runtime 的协调层；从每个 Job immutable
  manifest 的 Provider snapshot 创建对应角色，按 checkpoint 恢复并停止在未提交的 validated replacement；
  对只有 request、没有 response/plan/replacement 的 terminal `PROVIDER_UNAVAILABLE`，校验完整 append-only
  evidence 后最多追加一次新 attempt，并切换到新的 session ID。
- `core/project_commit_runtime.py`：OCHES003 的 compile-first canonical serial group commit、逐组 impact、单次 final
  Reviewer、Human gate 和精确 replay；每个编号化 commit manifest 是对应 group 的唯一 authority switch，失败组
  不改变 current roots，已成功的先前 group 不回滚。
- `core/tool_session.py`：角色级顺序工具会话；每轮只接受一个调用，最多执行 3 次不同名只读检索和一次
  final submission，并把完整请求、响应、参数和结果按角色保存为 append-only transcript。
- `core/project_tools.py`：从一个经过 fingerprint/lineage 校验的 exact current Job snapshot 提供 9 个只读工具；
  复用现有 unit/index、Reviewer report、Spec baseline 和 repair records，不建立第二套 evidence authority；accepted
  formal plan 可成为 authority，rejected plan 仅在 exact receipt + transcript 配对后作为 untrusted failure history。
- `core/project_incremental.py`：语义单元、direct-lineage fingerprint、Stage 3 确定性组装、
  Reviewer local certificate 和只计算不执行的 impact analysis。
- `core/project_stage3.py` / `scripts/run_project_stage3.py`：从一个已完成 Stage 1/2 的 immutable
  source Job 运行隔离的 test-only Stage 3；输出不得 promotion、approval 或进入 EDA。
- `core/project_reviewer.py`：Spec-only public reviewer surface。
- `adapters/llm/`：configured OpenAI-compatible provider boundary；model identity 由 config 选择。
- `adapters/eda/`：trusted no-shell EDA boundary。
- `contracts/project/`：Project submission、internal manifest、mapping、candidate、unit/index/assembly、
  review、impact 和 report contracts。
- `tests/agent_runtime/`：current Project positive/negative tests。

## Development validation

```bash
source /tmp/dv_phase_e_venv/bin/activate
PYTHONPATH=/home/xinyu/dv \
python /home/xinyu/dv/scripts/validate_project_job_workflow.py
```

Self-test must use scripted/mock providers and prove no production Generator/Reviewer、Human decision、
legacy/new production Job or production Project EDA evidence occurred. The existing aggregate may run its
temporary-directory Verilator vertical-flow self-test; that result is framework test evidence only.

Self-test 只调用 scripted providers 和 fake compile runner。无可修复 ERROR 时进入
`AWAITING_HUMAN_REVIEW`；有可修复 ERROR 时先由 OCHES002 产生 `SCOPED_REPLACEMENT_VALIDATED`，再由 OCHES003
按 canonical groups 执行 build-only 编译、serial commit、impact 和 final review。失败 group 记录
`NOT_COMMITTED` 且不切换 current authority；全部 groups 到达终态后，无论 final report 为 CLEAN、ERROR 或 WARNING，
都只生成 Human review request/checkpoint 并进入 `AWAITING_HUMAN_REVIEW`。测试不执行 production EDA、真实 Human
decision 或 promotion。

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
