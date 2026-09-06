module dvfs_seq_fsm #(
  parameter int unsigned OPP_COUNT = 4
) (
  input  logic                    clk_i,
  input  logic                    reset_n_i,
  input  logic                    start_i,
  input  logic [1:0]              target_opp_i,
  input  logic [OPP_COUNT*32-1:0] opp_table_i,
  input  logic [15:0]             timeout_cycles_i,
  input  logic                    voltage_ack_i,
  input  logic                    voltage_stable_i,
  input  logic                    frequency_ack_i,
  input  logic                    pll_lock_i,
  output logic                    voltage_req_valid_o,
  output logic [15:0]             voltage_code_o,
  output logic                    frequency_req_valid_o,
  output logic [15:0]             frequency_code_o,
  output logic [1:0]              current_opp_o,
  output logic [1:0]              target_opp_o,
  output logic                    busy_o,
  output logic                    done_o,
  output logic                    error_valid_o,
  output logic [3:0]              error_code_o
);
  import rscu_pkg::*;

  typedef enum logic [2:0] {
    DVFS_IDLE,
    DVFS_WAIT_VOLT_UP,
    DVFS_WAIT_FREQ_UP,
    DVFS_WAIT_FREQ_DOWN,
    DVFS_WAIT_VOLT_DOWN
  } dvfs_state_e;

  dvfs_state_e state_q;
  logic [15:0] wait_count_q;
  logic [31:0] selected_opp;

  function automatic logic timeout_hit(
    input logic [15:0] count,
    input logic [15:0] limit
  );
    timeout_hit = (limit != 16'b0) && ((count + 16'd1) >= limit);
  endfunction

  always_comb selected_opp = opp_table_i[target_opp_i*32 +: 32];

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      state_q               <= DVFS_IDLE;
      wait_count_q          <= 16'b0;
      voltage_req_valid_o   <= 1'b0;
      voltage_code_o        <= 16'b0;
      frequency_req_valid_o <= 1'b0;
      frequency_code_o      <= 16'b0;
      current_opp_o         <= 2'b0;
      target_opp_o          <= 2'b0;
      done_o                <= 1'b0;
      error_valid_o         <= 1'b0;
      error_code_o          <= ERR_NONE;
    end else begin
      done_o        <= 1'b0;
      error_valid_o <= 1'b0;

      case (state_q)
        DVFS_IDLE: begin
          wait_count_q <= 16'b0;
          if (start_i) begin
            target_opp_o     <= target_opp_i;
            voltage_code_o   <= selected_opp[15:0];
            frequency_code_o <= selected_opp[31:16];
            if (target_opp_i == current_opp_o) begin
              done_o <= 1'b1;
            end else if (target_opp_i > current_opp_o) begin
              voltage_req_valid_o <= 1'b1;
              state_q             <= DVFS_WAIT_VOLT_UP;
            end else begin
              frequency_req_valid_o <= 1'b1;
              state_q               <= DVFS_WAIT_FREQ_DOWN;
            end
          end
        end

        DVFS_WAIT_VOLT_UP: begin
          if (voltage_ack_i && voltage_stable_i) begin
            voltage_req_valid_o   <= 1'b0;
            frequency_req_valid_o <= 1'b1;
            wait_count_q          <= 16'b0;
            state_q               <= DVFS_WAIT_FREQ_UP;
          end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
            voltage_req_valid_o <= 1'b0;
            state_q             <= DVFS_IDLE;
            error_valid_o       <= 1'b1;
            error_code_o        <= ERR_VOLTAGE_TIMEOUT;
          end else begin
            wait_count_q <= wait_count_q + 16'd1;
          end
        end

        DVFS_WAIT_FREQ_UP: begin
          if (frequency_ack_i && pll_lock_i) begin
            frequency_req_valid_o <= 1'b0;
            current_opp_o         <= target_opp_o;
            state_q               <= DVFS_IDLE;
            done_o                <= 1'b1;
          end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
            frequency_req_valid_o <= 1'b0;
            state_q               <= DVFS_IDLE;
            error_valid_o         <= 1'b1;
            error_code_o          <= ERR_PLL_TIMEOUT;
          end else begin
            wait_count_q <= wait_count_q + 16'd1;
          end
        end

        DVFS_WAIT_FREQ_DOWN: begin
          if (frequency_ack_i && pll_lock_i) begin
            frequency_req_valid_o <= 1'b0;
            voltage_req_valid_o   <= 1'b1;
            wait_count_q          <= 16'b0;
            state_q               <= DVFS_WAIT_VOLT_DOWN;
          end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
            frequency_req_valid_o <= 1'b0;
            state_q               <= DVFS_IDLE;
            error_valid_o         <= 1'b1;
            error_code_o          <= ERR_PLL_TIMEOUT;
          end else begin
            wait_count_q <= wait_count_q + 16'd1;
          end
        end

        DVFS_WAIT_VOLT_DOWN: begin
          if (voltage_ack_i && voltage_stable_i) begin
            voltage_req_valid_o <= 1'b0;
            current_opp_o       <= target_opp_o;
            state_q             <= DVFS_IDLE;
            done_o              <= 1'b1;
          end else if (timeout_hit(wait_count_q, timeout_cycles_i)) begin
            voltage_req_valid_o <= 1'b0;
            state_q             <= DVFS_IDLE;
            error_valid_o       <= 1'b1;
            error_code_o        <= ERR_VOLTAGE_TIMEOUT;
          end else begin
            wait_count_q <= wait_count_q + 16'd1;
          end
        end

        default: begin
          state_q               <= DVFS_IDLE;
          voltage_req_valid_o   <= 1'b0;
          frequency_req_valid_o <= 1'b0;
          error_valid_o         <= 1'b1;
          error_code_o          <= ERR_ILLEGAL_STATE;
        end
      endcase
    end
  end

  always_comb busy_o = (state_q != DVFS_IDLE);
endmodule
