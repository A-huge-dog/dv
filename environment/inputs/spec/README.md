# Enhanced RSCU Structured Specification Baseline

The Design and DV flows shall consume this directory, not the PCK600 TRM. The project-definition Markdown is authoritative for scope; this self-owned baseline defines the missing executable contracts required by Phase-1.

Files:

- `enhanced_rscu_req.md`: stable requirements and priorities.
- `enhanced_rscu_arch.md`: module/state/interface ownership.
- `enhanced_rscu.yaml`: machine-readable profile.
- `csr.yaml`: register contract.
- `interface_contract.md`: external signal protocol.
- `power_sequence.md`, `dvfs_sequence.md`: ordered FSM behavior.
- `error_timeout_model.md`: error/IRQ codes and timing.
- `traceability_matrix.csv`: Spec-to-RTL-to-DV map.
- `acceptance_criteria.md`: Q0/Q1/Q2/Q3 evidence rules.
- `open_issues.md`: explicit deferred Q4 work and unsupported features.
