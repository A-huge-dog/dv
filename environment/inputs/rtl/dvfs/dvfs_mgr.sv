module dvfs_mgr #(
  parameter int unsigned OPP_COUNT = 4
) (
  input  logic                    clk_i,
  input  logic                    reset_n_i,
  input  logic [15:0]             timeout_cycles_i,
  input  logic                    safe_to_transition_i,
  input  logic                    fw_req_valid_i,
  input  logic [2:0]              fw_req_opp_i,
  input  logic                    thermal_req_valid_i,
  input  logic [2:0]              thermal_req_opp_i,
  input  logic                    perf_req_valid_i,
  input  logic [2:0]              perf_req_opp_i,
  input  logic [OPP_COUNT*32-1:0] opp_table_i,
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
  output logic                    done_event_o,
  output logic                    error_event_o,
  output logic [3:0]              error_code_o
);
  import rscu_pkg::*;

  logic request_valid;
  logic [2:0] selected_target;
  logic seq_start;
  logic seq_done;
  logic seq_error;
  logic [3:0] seq_error_code;

  // REQ_DVFS_004: thermal > firmware > performance.
  always_comb begin
    request_valid  = 1'b0;
    selected_target = 3'b0;
    if (thermal_req_valid_i) begin
      request_valid   = 1'b1;
      selected_target = thermal_req_opp_i;
    end else if (fw_req_valid_i) begin
      request_valid   = 1'b1;
      selected_target = fw_req_opp_i;
    end else if (perf_req_valid_i) begin
      request_valid   = 1'b1;
      selected_target = perf_req_opp_i;
    end
  end

  always_comb begin
    seq_start     = request_valid && !busy_o && safe_to_transition_i &&
                    (selected_target < OPP_COUNT);
    done_event_o  = seq_done;
    error_event_o = seq_error;
    error_code_o  = seq_error_code;

    if (request_valid && busy_o) begin
      error_event_o = 1'b1;
      error_code_o  = ERR_BUSY;
    end else if (request_valid && (selected_target >= OPP_COUNT)) begin
      error_event_o = 1'b1;
      error_code_o  = ERR_ILLEGAL_OPP;
    end else if (request_valid && !safe_to_transition_i) begin
      error_event_o = 1'b1;
      error_code_o  = ERR_DVFS_UNSAFE;
    end
  end

  dvfs_seq_fsm #(
    .OPP_COUNT (OPP_COUNT)
  ) u_seq_fsm (
    .clk_i,
    .reset_n_i,
    .start_i                 (seq_start),
    .target_opp_i            (selected_target[1:0]),
    .opp_table_i,
    .timeout_cycles_i,
    .voltage_ack_i,
    .voltage_stable_i,
    .frequency_ack_i,
    .pll_lock_i,
    .voltage_req_valid_o,
    .voltage_code_o,
    .frequency_req_valid_o,
    .frequency_code_o,
    .current_opp_o,
    .target_opp_o,
    .busy_o,
    .done_o                  (seq_done),
    .error_valid_o           (seq_error),
    .error_code_o            (seq_error_code)
  );
endmodule
