module rscu_mock_agents(rscu_if vif);
  integer domain;

  always_ff @(posedge vif.pclk or negedge vif.presetn) begin
    if (!vif.presetn) begin
      vif.power_ack     <= 4'b0;
      vif.power_good    <= 4'b0;
      vif.clock_ack     <= 4'b0;
      vif.reset_done    <= 4'b0;
      vif.isolation_ack <= 4'hf;
      vif.retention_ack <= 4'b0;
      vif.voltage_ack   <= 1'b0;
      vif.voltage_stable <= 1'b0;
      vif.frequency_ack <= 1'b0;
      vif.pll_lock      <= 1'b0;
    end else begin
      for (domain = 0; domain < 4; domain = domain + 1) begin
        if (!vif.hold_power[domain]) begin
          vif.power_ack[domain]  <= vif.power_req[domain];
          vif.power_good[domain] <= vif.power_req[domain];
        end
        if (!vif.hold_clock[domain]) begin
          vif.clock_ack[domain] <= vif.clock_enable[domain];
        end
        if (!vif.hold_reset[domain]) begin
          vif.reset_done[domain] <= 1'b1;
        end
        if (!vif.hold_isolation[domain]) begin
          vif.isolation_ack[domain] <= vif.isolation[domain];
        end
        if (!vif.hold_retention[domain]) begin
          vif.retention_ack[domain] <= vif.retention[domain];
        end
      end

      if (!vif.hold_voltage) begin
        vif.voltage_ack    <= vif.voltage_req_valid;
        vif.voltage_stable <= vif.voltage_req_valid;
      end
      if (!vif.hold_frequency) begin
        vif.frequency_ack <= vif.frequency_req_valid;
        vif.pll_lock      <= vif.frequency_req_valid;
      end
    end
  end

  always_comb begin
    vif.client_idle = vif.mock_client_idle;
    vif.client_deny = vif.mock_client_deny;
  end
endmodule
