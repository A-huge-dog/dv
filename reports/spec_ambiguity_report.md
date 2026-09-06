# Enhanced RSCU Phase-1 Spec Ambiguity Report

No blocking Phase-1 specification ambiguity remains. The structured baseline
defines the interface, CSR semantics, states and legal transitions, dependency
rules, ordered power/DVFS behavior, timeout unit and boundary, error/IRQ model,
reset behavior, overrides, and standalone acceptance criteria.

The following reviewed items are deferred to Q4 or signoff work and do not
authorize invented Phase-1 behavior:

- real subsystem bus selection and asynchronous crossings;
- CDC/RDC synchronizers and signoff;
- UPF and technology-specific power/clock/retention/isolation cells;
- authoritative clock and I/O constraints;
- per-tile power-domain profiles and non-default GPR/OPP profiles;
- real compute/fabric/memory, regulator, and PLL protocols;
- IMC coverage closure and real-subsystem integration.

The authoritative disposition is in
`../../design_docs/spec/open_issues.md`; implementation consequences are in
`../../design_docs/design/spec_gap_report.md` and
`../../design_docs/design/unsupported_features.md`.
