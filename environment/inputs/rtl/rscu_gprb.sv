module rscu_gprb #(
  parameter int unsigned GPR_WORDS = 4
) (
  input  logic                       clk_i,
  input  logic                       reset_n_i,
  input  logic                       write_valid_i,
  input  logic [1:0]                 write_index_i,
  input  logic [31:0]                write_data_i,
  input  logic [GPR_WORDS*32-1:0]    status_i,
  output logic [GPR_WORDS*32-1:0]    control_o,
  output logic [GPR_WORDS*32-1:0]    status_o
);
  logic [31:0] control_q [0:GPR_WORDS-1];
  integer index;

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      for (index = 0; index < GPR_WORDS; index = index + 1) begin
        control_q[index] <= 32'b0;
      end
    end else if (write_valid_i) begin
      control_q[write_index_i] <= write_data_i;
    end
  end

  generate
    genvar g;
    for (g = 0; g < GPR_WORDS; g = g + 1) begin : gen_gpr_control
      always_comb begin
        control_o[g*32 +: 32] = control_q[g];
        status_o[g*32 +: 32]  = status_i[g*32 +: 32];
      end
    end
  endgenerate
endmodule
