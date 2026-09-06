module rscu_sva(rscu_if vif);
  default clocking cb @(posedge vif.pclk); endclocking
  default disable iff (!vif.presetn);

  apb_ready: assert property (vif.psel && vif.penable |-> vif.pready)
    else $error("REQ_SYS_003: APB ACCESS did not complete");

  voltage_payload_stable: assert property (
    vif.voltage_req_valid && !(vif.voltage_ack && vif.voltage_stable)
    |=> $stable(vif.voltage_code))
    else $error("REQ_DVFS_006: voltage payload changed while waiting");

  frequency_payload_stable: assert property (
    vif.frequency_req_valid && !(vif.frequency_ack && vif.pll_lock)
    |=> $stable(vif.frequency_code))
    else $error("REQ_DVFS_006: frequency payload changed while waiting");

  override_controls: assert property (vif.test_override |->
    (vif.power_req == 4'hf) && (vif.clock_enable == 4'hf) &&
    (vif.domain_reset_n == 4'hf) && (vif.isolation == 4'h0) &&
    (vif.retention == 4'h0))
    else $error("REQ_DFT_001: test override controls are not forced");

  no_unknown_state: assert property (!$isunknown(vif.domain_state))
    else $error("REQ_DBG_001: domain state contains X/Z");

  generate
    genvar domain;
    for (domain = 0; domain < 4; domain = domain + 1) begin : gen_reset_safe
      reset_safe: assert property (
        !vif.presetn |-> !vif.power_req[domain] &&
        !vif.clock_enable[domain] && !vif.domain_reset_n[domain] &&
        vif.isolation[domain] && !vif.retention[domain])
        else $error("REQ_PWR_001: domain %0d reset controls unsafe", domain);

      deny_before_destructive_action: assert property (
        vif.quiesce_req[domain] && vif.client_deny[domain] |=>
        vif.power_req[domain] && vif.clock_enable[domain] &&
        vif.domain_reset_n[domain] && !vif.isolation[domain] &&
        !vif.retention[domain])
        else $error("REQ_QHS_001: domain %0d deny changed destructive controls", domain);
    end
  endgenerate
endmodule

module rscu_intr_handler_sva (
  input logic        clk_i,
  input logic        reset_n_i,
  input logic [15:0] event_set_i,
  input logic [15:0] clear_i,
  input logic [15:0] raw_o
);
  default clocking cb @(posedge clk_i); endclocking
  default disable iff (!reset_n_i);

  set_wins_simultaneous_clear: assert property (
    |(event_set_i & clear_i) |=>
    ((raw_o & $past(event_set_i & clear_i)) ==
     $past(event_set_i & clear_i)))
    else $error("REQ_IRQ_002: hardware event did not win simultaneous W1C clear");
endmodule

bind rscu_intr_handler rscu_intr_handler_sva u_rscu_intr_handler_sva (
  .clk_i,
  .reset_n_i,
  .event_set_i,
  .clear_i,
  .raw_o
);
