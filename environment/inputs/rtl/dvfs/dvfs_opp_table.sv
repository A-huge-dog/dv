module dvfs_opp_table #(
  parameter int unsigned OPP_COUNT = 4
) (
  input  logic                     clk_i,
  input  logic                     reset_n_i,
  input  logic                     write_valid_i,
  input  logic [1:0]               write_index_i,
  input  logic [31:0]              write_data_i,
  input  logic                     idle_i,
  output logic [OPP_COUNT*32-1:0]  opp_table_o
);
  logic [31:0] opp_q [0:OPP_COUNT-1];

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      // {frequency_code, voltage_code}; self-owned reference profile.
      opp_q[0] <= 32'h0190_0320;
      opp_q[1] <= 32'h0258_0352;
      opp_q[2] <= 32'h0320_0384;
      opp_q[3] <= 32'h03e8_03e8;
    end else if (write_valid_i && idle_i) begin
      opp_q[write_index_i] <= write_data_i;
    end
  end

  generate
    genvar g;
    for (g = 0; g < OPP_COUNT; g = g + 1) begin : gen_opp_flatten
      always_comb opp_table_o[g*32 +: 32] = opp_q[g];
    end
  endgenerate
endmodule
