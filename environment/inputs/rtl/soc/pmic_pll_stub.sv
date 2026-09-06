module pmic_pll_stub (
  input  logic clk_i,
  input  logic reset_n_i,
  input  logic voltage_req_valid_i,
  input  logic [15:0] voltage_code_i,
  input  logic frequency_req_valid_i,
  input  logic [15:0] frequency_code_i,
  output logic voltage_ack_o,
  output logic voltage_stable_o,
  output logic frequency_ack_o,
  output logic pll_lock_o,
  output logic [15:0] applied_voltage_o,
  output logic [15:0] applied_frequency_o
);
  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      voltage_ack_o       <= 1'b0;
      voltage_stable_o    <= 1'b0;
      frequency_ack_o     <= 1'b0;
      pll_lock_o          <= 1'b0;
      applied_voltage_o   <= 16'd800;
      applied_frequency_o <= 16'd400;
    end else begin
      voltage_ack_o    <= voltage_req_valid_i;
      voltage_stable_o <= voltage_req_valid_i;
      frequency_ack_o  <= frequency_req_valid_i;
      pll_lock_o       <= frequency_req_valid_i;
      if (voltage_req_valid_i) begin
        applied_voltage_o <= voltage_code_i;
      end
      if (frequency_req_valid_i) begin
        applied_frequency_o <= frequency_code_i;
      end
    end
  end
endmodule
