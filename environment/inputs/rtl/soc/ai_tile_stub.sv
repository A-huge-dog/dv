module ai_tile_stub #(
  parameter logic [7:0] TILE_ID = 8'd0
) (
  input  logic clk_i,
  input  logic reset_n_i,
  input  logic power_on_i,
  input  logic clock_enable_i,
  input  logic domain_reset_n_i,
  input  logic enable_i,
  input  logic start_i,
  output logic busy_o,
  output logic done_o,
  output logic [31:0] status_o
);
  logic start_d_q;
  logic [4:0] cycle_count_q;
  logic [15:0] result_q;

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      start_d_q    <= 1'b0;
      cycle_count_q <= 5'b0;
      result_q     <= 16'b0;
      busy_o       <= 1'b0;
      done_o       <= 1'b0;
    end else begin
      start_d_q <= start_i;
      done_o    <= 1'b0;
      if (!power_on_i || !domain_reset_n_i) begin
        cycle_count_q <= 5'b0;
        result_q      <= 16'b0;
        busy_o        <= 1'b0;
      end else if (clock_enable_i) begin
        if (enable_i && start_i && !start_d_q && !busy_o) begin
          cycle_count_q <= 5'd15;
          result_q      <= {8'b0, TILE_ID};
          busy_o        <= 1'b1;
        end else if (busy_o) begin
          result_q <= result_q + {8'b0, TILE_ID} + 16'd1;
          if (cycle_count_q == 5'b0) begin
            busy_o <= 1'b0;
            done_o <= 1'b1;
          end else begin
            cycle_count_q <= cycle_count_q - 5'd1;
          end
        end
      end
    end
  end

  always_comb status_o = {result_q, 6'b0, TILE_ID, done_o, busy_o};
endmodule
