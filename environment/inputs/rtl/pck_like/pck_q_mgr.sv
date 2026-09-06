module pck_q_mgr (
  input  logic        clk_i,
  input  logic        reset_n_i,
  input  logic        start_i,
  input  logic        pause_i,
  input  logic [15:0] timeout_cycles_i,
  input  logic        client_idle_i,
  input  logic        client_deny_i,
  output logic        request_o,
  output logic        active_o,
  output logic        done_o,
  output logic        denied_o,
  output logic        timeout_o
);
  logic [15:0] wait_count_q;

  function automatic logic timeout_hit(
    input logic [15:0] count,
    input logic [15:0] limit
  );
    timeout_hit = (limit != 16'b0) && ((count + 16'd1) >= limit);
  endfunction

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      active_o    <= 1'b0;
      done_o      <= 1'b0;
      denied_o    <= 1'b0;
      timeout_o   <= 1'b0;
      wait_count_q <= 16'b0;
    end else begin
      done_o    <= 1'b0;
      denied_o  <= 1'b0;
      timeout_o <= 1'b0;
      if (!pause_i) begin
        if (start_i && !active_o) begin
          active_o     <= 1'b1;
          wait_count_q <= 16'b0;
        end else if (active_o) begin
          if (client_deny_i) begin
            active_o <= 1'b0;
            denied_o <= 1'b1;
          end else if (client_idle_i) begin
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

  always_comb request_o = active_o;
endmodule
