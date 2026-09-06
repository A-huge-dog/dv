# EDA adapters

## Isolated Xcelium adapter

`XceliumAdapter` is an isolated execution boundary for an approved `xrun`
executable. It owns no Project authority itself. ProjectLoop calls it only
through two scoped boundaries: the pre-Human UVM Worker may build the current
UVM candidate with a Framework-owned elaboration harness and no RTL, while
PJ-003 may build/run only after the separate Human authorization and binding.

It provides three operations:

- `probe_version()` runs `xrun -version` and checks an approved version pattern;
- `build_only()` compiles and elaborates one exact source set;
- `run()` performs a fresh compile, elaboration, and simulation in one isolated
  run directory.

Each operation persists an immutable typed request and evidence pair under:

```text
result/jobs/<job_id>/
├── audit/xcelium/
└── runs/xcelium/
```

Evidence uses the qualification scope
`ISOLATED_XCELIUM_ADAPTER_NO_PROJECT_AUTHORITY`. It cannot be interpreted as
testcase approval, Project execution, DUT pass, PJ-003 completion, or full UVM
qualification.

### Environment boundary

The deployment environment must already contain the Xcelium installation and
license setup. The adapter never runs `module load`, a shell, or a setup script.
Only explicitly allowed names are copied into the private child environment:

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

For a UVM testcase, the preloaded environment must set `UVMHOME` to the
installed Cadence UVM SystemVerilog root.  The adapter passes that approved
value unchanged to `xrun`; it does not infer a UVM installation path.

Their values are not stored in requests or evidence. `HOME` and `TMPDIR` are
replaced with Job-confined directories for every invocation.

### Example

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

The current implementation deliberately uses a fresh one-step `xrun` flow for
`run()`. It does not reuse the database produced by `build_only()`: that avoids
silently crossing build/run authority before PJ-003 defines the approved
binding and snapshot-reuse contract.
