`timescale 1ns/1ps

module tb_top;
  import uvm_pkg::*;
  import rscu_uvm_pkg::*;

  logic pclk;
  rscu_if vif(pclk);

  initial pclk = 1'b0;
  always #5ns pclk = ~pclk;

  enhanced_rscu_top dut (
    .pclk,
    .presetn                  (vif.presetn),
    .psel                     (vif.psel),
    .penable                  (vif.penable),
    .pwrite                   (vif.pwrite),
    .paddr                    (vif.paddr),
    .pwdata                   (vif.pwdata),
    .prdata                   (vif.prdata),
    .pready                   (vif.pready),
    .pslverr                  (vif.pslverr),
    .client_idle_i            (vif.client_idle),
    .client_deny_i            (vif.client_deny),
    .power_ack_i              (vif.power_ack),
    .power_good_i             (vif.power_good),
    .clock_ack_i              (vif.clock_ack),
    .reset_done_i             (vif.reset_done),
    .isolation_ack_i          (vif.isolation_ack),
    .retention_ack_i          (vif.retention_ack),
    .quiesce_req_o            (vif.quiesce_req),
    .power_req_o              (vif.power_req),
    .clock_enable_o           (vif.clock_enable),
    .reset_n_o                (vif.domain_reset_n),
    .isolation_o              (vif.isolation),
    .retention_o              (vif.retention),
    .thermal_req_valid_i      (vif.thermal_req_valid),
    .thermal_req_opp_i        (vif.thermal_req_opp),
    .perf_req_valid_i         (vif.perf_req_valid),
    .perf_req_opp_i           (vif.perf_req_opp),
    .voltage_req_valid_o      (vif.voltage_req_valid),
    .voltage_code_o           (vif.voltage_code),
    .voltage_ack_i            (vif.voltage_ack),
    .voltage_stable_i         (vif.voltage_stable),
    .frequency_req_valid_o    (vif.frequency_req_valid),
    .frequency_code_o         (vif.frequency_code),
    .frequency_ack_i          (vif.frequency_ack),
    .pll_lock_i               (vif.pll_lock),
    .gpr_status_i             (vif.gpr_status),
    .gpr_control_o            (vif.gpr_control),
    .test_override_i          (vif.test_override),
    .irq_o                    (vif.irq),
    .domain_state_o           (vif.domain_state),
    .current_opp_o            (vif.current_opp)
  );

  rscu_mock_agents u_mock_agents(vif);
  rscu_sva u_sva(vif);
  rscu_coverage u_coverage(vif);

  initial begin
    uvm_config_db#(virtual rscu_if)::set(null, "*", "vif", vif);
    run_test();
  end
endmodule
