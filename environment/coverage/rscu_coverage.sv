module rscu_coverage(rscu_if vif);
  covergroup rscu_cg @(posedge vif.pclk);
    option.per_instance = 1;
    cp_apb_addr: coverpoint vif.paddr iff (vif.psel && vif.penable) {
      bins identification = {12'h000, 12'h004, 12'h008};
      bins irq_regs[] = {[12'h010:12'h020]};
      bins power_cmd = {12'h080};
      bins power_status[] = {12'h084, 12'h088, 12'h08c};
      bins dvfs_cmd = {12'h100};
      bins dvfs_status = {12'h104};
      bins opp_regs[] = {[12'h110:12'h11c]};
    }
    cp_write: coverpoint vif.pwrite iff (vif.psel && vif.penable);
    cp_error: coverpoint vif.pslverr iff (vif.psel && vif.penable);
    cp_compute_state: coverpoint vif.domain_state[1:0] {
      bins off = {0}; bins retention = {1}; bins on = {2}; bins error_state = {3};
      bins off_to_on = (0 => 2);
      bins on_to_retention = (2 => 1);
      bins retention_to_on = (1 => 2);
      bins on_to_off = (2 => 0);
      bins error_to_off = (3 => 0);
    }
    cp_opp: coverpoint vif.current_opp { bins opp[] = {[0:3]}; }
    cp_irq: coverpoint vif.irq;
    cp_quiesce: coverpoint (|vif.quiesce_req);
    cp_voltage_request: coverpoint vif.voltage_req_valid;
    cp_frequency_request: coverpoint vif.frequency_req_valid;
    cp_override: coverpoint vif.test_override;
    cross_apb_access: cross cp_apb_addr, cp_write;
  endgroup

  rscu_cg cg;
  initial cg = new();
endmodule
