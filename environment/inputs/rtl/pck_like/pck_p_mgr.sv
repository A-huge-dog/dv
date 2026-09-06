module pck_p_mgr (
  input  logic        clk_i,
  input  logic        reset_n_i,
  input  logic        start_i,
  input  logic        target_on_i,
  input  logic        force_off_i,
  input  logic        pause_i,
  input  logic [15:0] timeout_cycles_i,
  input  logic        power_ack_i,
  input  logic        power_good_i,
  output logic        power_request_o,
  output logic        active_o,
  output logic        done_o,
  output logic        timeout_o
);
  logic [15:0] wait_count_q;
  logic target_on_q;

  function automatic logic timeout_hit(
    input logic [15:0] count,
    input logic [15:0] limit
  );
    timeout_hit = (limit != 16'b0) && ((count + 16'd1) >= limit);
  endfunction

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      power_request_o <= 1'b0;
      active_o        <= 1'b0;
      done_o          <= 1'b0;
      timeout_o       <= 1'b0;
      wait_count_q    <= 16'b0;
      target_on_q     <= 1'b0;
    end else begin
      done_o    <= 1'b0;
      timeout_o <= 1'b0;
      if (force_off_i) begin
        power_request_o <= 1'b0;
        active_o        <= 1'b0;
        wait_count_q    <= 16'b0;
        target_on_q     <= 1'b0;
      end else if (!pause_i) begin
        if (start_i && !active_o) begin
          power_request_o <= target_on_i;
          target_on_q     <= target_on_i;
          active_o        <= 1'b1;
          wait_count_q    <= 16'b0;
        end else if (active_o) begin
          if ((power_ack_i == target_on_q) &&
              (power_good_i == target_on_q)) begin
            active_o <= 1'b0;
            done_o   <= 1'b1;
          end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
            active_o  <= 1'b0;
            timeout_o <= 1'b1;
          end else begin
            wait_count_q <= wait_count_q + 16'd1;
          end
        end
      end
    end
  end
endmodule
