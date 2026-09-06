# RTL Feature Configuration Audit

Audited input: `inputs/rtl` frozen snapshot.

- No `ifdef`, `ifndef`, or externally selected RTL feature macro exists.
- `xcelium.json` therefore selects an empty `defines` list.
- Reference behavior is selected by elaboration parameters at their authoritative defaults: `NUM_DOMAINS=4`, `GPR_WORDS=4`, `OPP_COUNT=4`, and `DEP_MASK_RESET=16'h0886`.
- The file order follows package/import and instantiation dependencies; `rscu_pkg.sv` precedes all consumers and `enhanced_rscu_top.sv` follows leaf modules.
- DUT top is `enhanced_rscu_top`; DV top is `tb_top`.
- No alternate define profile changes product behavior, so the feature-configuration gate is unambiguous.
