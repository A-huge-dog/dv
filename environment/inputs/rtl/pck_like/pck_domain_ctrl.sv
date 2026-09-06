module pck_domain_ctrl (
  input  logic        clk_i,
  input  logic        reset_n_i,
  input  logic        start_i,
  input  logic [1:0]  target_state_i,
  input  logic        dependency_ok_i,
  input  logic [15:0] timeout_cycles_i,
  input  logic        test_override_i,

  input  logic client_idle_i,
  input  logic client_deny_i,
  input  logic power_ack_i,
  input  logic power_good_i,
  input  logic clock_ack_i,
  input  logic reset_done_i,
  input  logic isolation_ack_i,
  input  logic retention_ack_i,

  output logic quiesce_req_o,
  output logic power_req_o,
  output logic clock_enable_o,
  output logic reset_n_o,
  output logic isolation_o,
  output logic retention_o,
  output logic [1:0] current_state_o,
  output logic       busy_o,
  output logic       done_o,
  output logic       error_valid_o,
  output logic [3:0] error_code_o
);
  import rscu_pkg::*;

  typedef enum logic [3:0] {
    ST_IDLE,
    ST_START_Q,
    ST_WAIT_Q,
    ST_WAIT_CLOCK_OFF,
    ST_WAIT_RESET_ASSERT,
    ST_WAIT_ISO_ON,
    ST_WAIT_RET_ON,
    ST_START_POWER_OFF,
    ST_WAIT_POWER_OFF,
    ST_START_POWER_ON,
    ST_WAIT_POWER_ON,
    ST_WAIT_RET_OFF,
    ST_WAIT_ISO_OFF,
    ST_WAIT_CLOCK_ON,
    ST_WAIT_RESET_RELEASE
  } seq_state_e;

  seq_state_e state_q;
  pwr_state_e current_state_q;
  logic [1:0] operation_target_q;
  logic [15:0] wait_count_q;
  logic clock_enable_q;
  logic reset_n_q;
  logic isolation_q;
  logic retention_q;

  logic q_start;
  logic q_active;
  logic q_done;
  logic q_denied;
  logic q_timeout;
  logic p_start;
  logic p_target_on;
  logic p_force_off_q;
  logic p_active;
  logic p_done;
  logic p_timeout;

  function automatic logic timeout_hit(
    input logic [15:0] count,
    input logic [15:0] limit
  );
    timeout_hit = (limit != 16'b0) && ((count + 16'd1) >= limit);
  endfunction

  always_comb begin
    q_start     = (state_q == ST_START_Q);
    p_start     = (state_q == ST_START_POWER_ON) ||
                  (state_q == ST_START_POWER_OFF);
    p_target_on = (state_q == ST_START_POWER_ON);
  end

  pck_q_mgr u_q_mgr (
    .clk_i,
    .reset_n_i,
    .start_i          (q_start),
    .pause_i          (test_override_i),
    .timeout_cycles_i,
    .client_idle_i,
    .client_deny_i,
    .request_o        (quiesce_req_o),
    .active_o         (q_active),
    .done_o           (q_done),
    .denied_o         (q_denied),
    .timeout_o        (q_timeout)
  );

  pck_p_mgr u_p_mgr (
    .clk_i,
    .reset_n_i,
    .start_i          (p_start),
    .target_on_i      (p_target_on),
    .force_off_i      (p_force_off_q),
    .pause_i          (test_override_i),
    .timeout_cycles_i,
    .power_ack_i,
    .power_good_i,
    .power_request_o  (power_req_o),
    .active_o         (p_active),
    .done_o           (p_done),
    .timeout_o        (p_timeout)
  );

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      state_q            <= ST_IDLE;
      current_state_q    <= PWR_OFF;
      operation_target_q <= PWR_OFF;
      wait_count_q       <= 16'b0;
      clock_enable_q     <= 1'b0;
      reset_n_q          <= 1'b0;
      isolation_q        <= 1'b1;
      retention_q        <= 1'b0;
      done_o             <= 1'b0;
      error_valid_o      <= 1'b0;
      error_code_o       <= ERR_NONE;
      p_force_off_q      <= 1'b0;
    end else begin
      done_o        <= 1'b0;
      error_valid_o <= 1'b0;
      p_force_off_q <= 1'b0;

      // REQ_DFT_001: pause functional progression while final output mux is forced.
      if (!test_override_i) begin
        case (state_q)
          ST_IDLE: begin
            wait_count_q <= 16'b0;
            if (start_i) begin
              if (target_state_i == current_state_q) begin
                done_o <= 1'b1;
              end else if ((current_state_q == PWR_ERROR) &&
                           (target_state_i == PWR_OFF)) begin
                operation_target_q <= PWR_OFF;
                clock_enable_q     <= 1'b0;
                reset_n_q          <= 1'b0;
                isolation_q        <= 1'b1;
                retention_q        <= 1'b0;
                state_q            <= ST_START_POWER_OFF;
              end else if ((target_state_i == PWR_ERROR) ||
                           ((current_state_q == PWR_OFF) &&
                            (target_state_i == PWR_RETENTION)) ||
                           ((current_state_q == PWR_ERROR) &&
                            (target_state_i != PWR_OFF))) begin
                error_valid_o <= 1'b1;
                error_code_o  <= ERR_ILLEGAL_STATE;
              end else if (!dependency_ok_i) begin
                error_valid_o <= 1'b1;
                error_code_o  <= ERR_DEPENDENCY;
              end else begin
                operation_target_q <= target_state_i;
                if (target_state_i == PWR_ON) begin
                  state_q <= ST_START_POWER_ON;
                end else if (current_state_q == PWR_ON) begin
                  state_q <= ST_START_Q;
                end else begin
                  // Only RETENTION to OFF remains here.
                  retention_q  <= 1'b0;
                  wait_count_q <= 16'b0;
                  state_q      <= ST_WAIT_RET_OFF;
                end
              end
            end
          end

          ST_START_Q: begin
            state_q <= ST_WAIT_Q;
          end

          ST_WAIT_Q: begin
            if (q_denied) begin
              state_q       <= ST_IDLE;
              error_valid_o <= 1'b1;
              error_code_o  <= ERR_QUIESCE_DENY;
            end else if (q_timeout) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_QUIESCE_TIMEOUT;
            end else if (q_done) begin
              clock_enable_q <= 1'b0;
              wait_count_q   <= 16'b0;
              state_q        <= ST_WAIT_CLOCK_OFF;
            end
          end

          ST_WAIT_CLOCK_OFF: begin
            if (!clock_ack_i) begin
              reset_n_q    <= 1'b0;
              wait_count_q <= 16'b0;
              state_q      <= ST_WAIT_RESET_ASSERT;
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_CLOCK_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_WAIT_RESET_ASSERT: begin
            if (reset_done_i) begin
              isolation_q  <= 1'b1;
              wait_count_q <= 16'b0;
              state_q      <= ST_WAIT_ISO_ON;
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_RESET_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_WAIT_ISO_ON: begin
            if (isolation_ack_i) begin
              wait_count_q <= 16'b0;
              if (operation_target_q == PWR_RETENTION) begin
                retention_q <= 1'b1;
                state_q     <= ST_WAIT_RET_ON;
              end else begin
                state_q <= ST_START_POWER_OFF;
              end
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_ISOLATION_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_WAIT_RET_ON: begin
            if (retention_ack_i) begin
              wait_count_q <= 16'b0;
              state_q      <= ST_START_POWER_OFF;
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_RETENTION_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_START_POWER_OFF: begin
            state_q <= ST_WAIT_POWER_OFF;
          end

          ST_WAIT_POWER_OFF: begin
            if (p_timeout) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_POWER_TIMEOUT;
            end else if (p_done) begin
              current_state_q <= pwr_state_e'(operation_target_q);
              state_q         <= ST_IDLE;
              done_o          <= 1'b1;
            end
          end

          ST_START_POWER_ON: begin
            state_q <= ST_WAIT_POWER_ON;
          end

          ST_WAIT_POWER_ON: begin
            if (p_timeout) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_POWER_TIMEOUT;
            end else if (p_done) begin
              wait_count_q <= 16'b0;
              if (current_state_q == PWR_RETENTION) begin
                retention_q <= 1'b0;
                state_q     <= ST_WAIT_RET_OFF;
              end else begin
                isolation_q <= 1'b0;
                state_q     <= ST_WAIT_ISO_OFF;
              end
            end
          end

          ST_WAIT_RET_OFF: begin
            if (!retention_ack_i) begin
              wait_count_q <= 16'b0;
              if (operation_target_q == PWR_OFF) begin
                current_state_q <= PWR_OFF;
                state_q         <= ST_IDLE;
                done_o          <= 1'b1;
              end else begin
                isolation_q <= 1'b0;
                state_q     <= ST_WAIT_ISO_OFF;
              end
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_RETENTION_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_WAIT_ISO_OFF: begin
            if (!isolation_ack_i) begin
              clock_enable_q <= 1'b1;
              wait_count_q   <= 16'b0;
              state_q        <= ST_WAIT_CLOCK_ON;
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_ISOLATION_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_WAIT_CLOCK_ON: begin
            if (clock_ack_i) begin
              reset_n_q    <= 1'b1;
              wait_count_q <= 16'b0;
              state_q      <= ST_WAIT_RESET_RELEASE;
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_CLOCK_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          ST_WAIT_RESET_RELEASE: begin
            if (reset_done_i) begin
              current_state_q <= PWR_ON;
              state_q         <= ST_IDLE;
              done_o          <= 1'b1;
            end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
              state_q         <= ST_IDLE;
              current_state_q <= PWR_ERROR;
              clock_enable_q  <= 1'b0;
              reset_n_q       <= 1'b0;
              isolation_q     <= 1'b1;
              retention_q     <= 1'b0;
              p_force_off_q   <= 1'b1;
              error_valid_o   <= 1'b1;
              error_code_o    <= ERR_RESET_TIMEOUT;
            end else begin
              wait_count_q <= wait_count_q + 16'd1;
            end
          end

          default: begin
            state_q         <= ST_IDLE;
            current_state_q <= PWR_ERROR;
            clock_enable_q  <= 1'b0;
            reset_n_q       <= 1'b0;
            isolation_q     <= 1'b1;
            retention_q     <= 1'b0;
            p_force_off_q   <= 1'b1;
            error_valid_o   <= 1'b1;
            error_code_o    <= ERR_ILLEGAL_STATE;
          end
        endcase
      end else if (start_i) begin
        error_valid_o <= 1'b1;
        error_code_o  <= ERR_BUSY;
      end
    end
  end

  always_comb begin
    clock_enable_o = clock_enable_q;
    reset_n_o      = reset_n_q;
    isolation_o    = isolation_q;
    retention_o    = retention_q;
    current_state_o = current_state_q;
    busy_o = (state_q != ST_IDLE) || q_active || p_active;
  end
endmodule
