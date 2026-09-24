# EDA 工具适配器

EDA（Electronic Design Automation，电子设计自动化）工具用于编译和仿真芯片设计。
本项目通过适配器调用这些工具，把项目流程与外部工具的具体执行方式分开。

## Xcelium 适配器

`XceliumAdapter` 接收源文件和运行配置，在隔离目录中调用预先配置的 `xrun`
可执行文件，并保存请求、日志和结果。适配器只负责执行，流程何时进入编译或仿真
由 `ProjectLoop`（项目流程控制器）决定：

1. 场景生成后，用户提交场景审核表。
2. UVM Worker（验证环境生成模块）生成并检查 UVM 环境。
   UVM（Universal Verification Methodology，通用验证方法学）提供组织测试和检查结果的标准方法。
   此时使用框架提供的 harness（最小检查顶层，用来连接验证环境并检查其能否编译和展开），
   不包含 RTL（Register Transfer Level，寄存器传输级描述，即被测芯片的硬件实现代码）。
3. 用例生成和评审结束后，`ProjectLoop` 自动冻结本次执行的输入、绑定 RTL、
   编译完整环境并逐个运行用例，无需再次提交人工批准或执行授权。

适配器提供以下核心执行操作：

- `probe_version()`：运行 `xrun -version`，检查版本是否符合配置要求。
- `compile_only()`：只编译指定源文件，不选择或展开顶层。
- `build_only()`：编译指定源文件并进行 elaboration（展开，即解析模块实例和连接关系）。
- `run()`：在独立运行目录中重新完成编译、展开和仿真。

这些操作会保存结构化请求及对应执行证据，已有记录不可覆盖。文件位于：

```text
result/jobs/<job_id>/
├── audit/xcelium/
└── runs/xcelium/
```

证据的适用范围标记为 `ISOLATED_XCELIUM_ADAPTER_NO_PROJECT_AUTHORITY`。
单次适配器操作成功仅说明该操作通过，不能单独代表用例审核通过、完整项目执行完成、
DUT（Design Under Test，被测设计，即本次验证的芯片模块）正确，或 UVM 环境已经得到全面验证。

`merge_coverage()` 还可调用同一安装环境中的 `imc` 工具，合并当前任务的覆盖率数据库；
覆盖率表示测试执行时哪些设计逻辑或预设测试目标已被触发。

### 运行环境

启动项目前应完成 Xcelium 安装和许可证配置。适配器不会执行 `module load`、
命令解释器或环境配置脚本，只允许将以下变量复制到工具进程的私有环境：

```text
PATH
LD_LIBRARY_PATH
CDS_LIC_FILE
LM_LICENSE_FILE
CDS_ROOT
UVMHOME
XCELIUMHOME
LC_ALL
```

运行 UVM 用例前，必须将 `UVMHOME` 设置为已安装的 Cadence UVM SystemVerilog
根目录。适配器会将这个值原样传给 `xrun`，不会自行推断 UVM 的安装路径。

环境变量的值不写入请求或执行证据。每次调用时，`HOME` 和 `TMPDIR`
都会指向当前任务专属的目录。

### 调用示例

```python
from pathlib import Path

from adapters.eda import XceliumAdapter, XceliumRunConfiguration

adapter = XceliumAdapter.from_preloaded_environment(
    workspace_root=Path("/workspace"),
    result_root=Path("/workspace/result"),
    job_id="JOB.XCELIUM.ISOLATED.SMOKE",
    environment_identity="XCELIUMENV.LAB.24_09",
)

configuration = XceliumRunConfiguration(
    sources=("rtl/dut.sv", "tb/tb_top.sv"),
    top="tb_top",
    include_dirs=("tb",),
    uvm=True,
    uvm_test="base_test",
    seed=1,
    coverage=True,
    waves=True,
    pass_marker="DV_XCELIUM_SMOKE_PASS",
)

version = adapter.probe_version()
build = adapter.build_only("BUILD.SMOKE", configuration)
run = adapter.run("RUN.SMOKE", configuration)
```

`run()` 每次通过一次新的 `xrun` 调用完成编译、展开和仿真，不复用
`build_only()` 生成的数据库。因此，项目先完成一次整体编译检查，随后每个用例
运行时仍会重新编译和展开；整体编译成功不会省去这些步骤。
