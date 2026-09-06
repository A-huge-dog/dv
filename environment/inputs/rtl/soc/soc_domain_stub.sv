module soc_domain_stub (
  input  logic clk_i,
  input  logic reset_n_i,
  input  logic workload_busy_i,
  input  logic quiesce_req_i,
  input  logic power_req_i,
  input  logic clock_enable_i,
  input  logic domain_reset_n_i,
  input  logic isolation_i,
  input  logic retention_i,
  output logic client_idle_o,
  output logic client_deny_o,
  output logic power_ack_o,
  output logic power_good_o,
  output logic clock_ack_o,
  output logic reset_done_o,
  output logic isolation_ack_o,
  output logic retention_ack_o
);
  logic unused_quiesce;

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      power_ack_o     <= 1'b0;
      power_good_o    <= 1'b0;
      clock_ack_o     <= 1'b0;
      reset_done_o    <= 1'b0;
      isolation_ack_o <= 1'b1;
      retention_ack_o <= 1'b0;
    end else begin
      power_ack_o     <= power_req_i;
      power_good_o    <= power_req_i;
      clock_ack_o     <= clock_enable_i;
      reset_done_o    <= 1'b1;
      isolation_ack_o <= isolation_i;
      retention_ack_o <= retention_i;
    end
  end

  always_comb begin
    client_idle_o = !workload_busy_i;
    client_deny_o = 1'b0;
    unused_quiesce = quiesce_req_i ^ domain_reset_n_i;
  end
endmodule
