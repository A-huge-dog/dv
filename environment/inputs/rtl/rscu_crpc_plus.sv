module rscu_crpc_plus #(
  parameter int unsigned NUM_DOMAINS = 4,
  parameter logic [NUM_DOMAINS*NUM_DOMAINS-1:0] DEP_MASK_RESET = 16'h0886
) (
  input  logic clk_i,
  input  logic reset_n_i,
  input  logic [15:0] timeout_cycles_i,
  input  logic test_override_i,
  input  logic cmd_valid_i,
  input  logic [2:0] cmd_domain_i,
  input  logic [1:0] cmd_target_i,

  input  logic [NUM_DOMAINS-1:0] client_idle_i,
  input  logic [NUM_DOMAINS-1:0] client_deny_i,
  input  logic [NUM_DOMAINS-1:0] power_ack_i,
  input  logic [NUM_DOMAINS-1:0] power_good_i,
  input  logic [NUM_DOMAINS-1:0] clock_ack_i,
  input  logic [NUM_DOMAINS-1:0] reset_done_i,
  input  logic [NUM_DOMAINS-1:0] isolation_ack_i,
  input  logic [NUM_DOMAINS-1:0] retention_ack_i,

  output logic [NUM_DOMAINS-1:0] quiesce_req_o,
  output logic [NUM_DOMAINS-1:0] power_req_o,
  output logic [NUM_DOMAINS-1:0] clock_enable_o,
  output logic [NUM_DOMAINS-1:0] reset_n_o,
  output logic [NUM_DOMAINS-1:0] isolation_o,
  output logic [NUM_DOMAINS-1:0] retention_o,
  output logic [NUM_DOMAINS*2-1:0] domain_state_o,
  output logic [NUM_DOMAINS-1:0] domain_busy_o,
  output logic [NUM_DOMAINS*4-1:0] domain_error_code_o,
  output logic [NUM_DOMAINS*NUM_DOMAINS-1:0] dependency_masks_o,
  output logic done_event_o,
  output logic error_event_o,
  output logic [2:0] event_domain_o,
  output logic [3:0] event_error_code_o
);
  import rscu_pkg::*;

  logic dependency_legal;
  logic command_id_valid;
  logic command_busy;
  logic command_accept;
  logic [NUM_DOMAINS-1:0] domain_start;
  logic [NUM_DOMAINS-1:0] functional_quiesce;
  logic [NUM_DOMAINS-1:0] functional_power;
  logic [NUM_DOMAINS-1:0] functional_clock;
  logic [NUM_DOMAINS-1:0] functional_reset_n;
  logic [NUM_DOMAINS-1:0] functional_isolation;
  logic [NUM_DOMAINS-1:0] functional_retention;
  logic [NUM_DOMAINS-1:0] domain_done;
  logic [NUM_DOMAINS-1:0] domain_error_valid;
  integer event_index;

  always_comb begin
    command_id_valid = (cmd_domain_i < NUM_DOMAINS);
    command_busy = 1'b0;
    if (command_id_valid) begin
      command_busy = domain_busy_o[cmd_domain_i];
    end
    command_accept = cmd_valid_i && command_id_valid && !command_busy;
    dependency_masks_o = DEP_MASK_RESET;
  end

  pck_dependency_checker #(
    .NUM_DOMAINS (NUM_DOMAINS)
  ) u_dependency_checker (
    .domain_id_i     (cmd_domain_i),
    .target_state_i  (cmd_target_i),
    .domain_state_i  (domain_state_o),
    .parent_masks_i  (DEP_MASK_RESET),
    .legal_o         (dependency_legal)
  );

  generate
    genvar g;
    for (g = 0; g < NUM_DOMAINS; g = g + 1) begin : gen_domain_ctrl
      always_comb domain_start[g] = command_accept && (cmd_domain_i == g);

      pck_domain_ctrl u_domain_ctrl (
        .clk_i,
        .reset_n_i,
        .start_i           (domain_start[g]),
        .target_state_i    (cmd_target_i),
        .dependency_ok_i   (dependency_legal),
        .timeout_cycles_i,
        .test_override_i,
        .client_idle_i     (client_idle_i[g]),
        .client_deny_i     (client_deny_i[g]),
        .power_ack_i       (power_ack_i[g]),
        .power_good_i      (power_good_i[g]),
        .clock_ack_i       (clock_ack_i[g]),
        .reset_done_i      (reset_done_i[g]),
        .isolation_ack_i   (isolation_ack_i[g]),
        .retention_ack_i   (retention_ack_i[g]),
        .quiesce_req_o     (functional_quiesce[g]),
        .power_req_o       (functional_power[g]),
        .clock_enable_o    (functional_clock[g]),
        .reset_n_o         (functional_reset_n[g]),
        .isolation_o       (functional_isolation[g]),
        .retention_o       (functional_retention[g]),
        .current_state_o   (domain_state_o[g*2 +: 2]),
        .busy_o            (domain_busy_o[g]),
        .done_o            (domain_done[g]),
        .error_valid_o     (domain_error_valid[g]),
        .error_code_o      (domain_error_code_o[g*4 +: 4])
      );
    end
  endgenerate

  // REQ_DFT_001: final request mux; functional state remains frozen in children.
  always_comb begin
    if (test_override_i) begin
      quiesce_req_o = '0;
      power_req_o   = '1;
      clock_enable_o = '1;
      reset_n_o     = '1;
      isolation_o   = '0;
      retention_o   = '0;
    end else begin
      quiesce_req_o = functional_quiesce;
      power_req_o   = functional_power;
      clock_enable_o = functional_clock;
      reset_n_o     = functional_reset_n;
      isolation_o   = functional_isolation;
      retention_o   = functional_retention;
    end
  end

  always_comb begin
    done_event_o       = |domain_done;
    error_event_o      = 1'b0;
    event_domain_o     = 3'b0;
    event_error_code_o = ERR_NONE;

    for (event_index = 0; event_index < NUM_DOMAINS; event_index = event_index + 1) begin
      if (domain_done[event_index]) begin
        event_domain_o = event_index[2:0];
      end
      if (domain_error_valid[event_index]) begin
        error_event_o      = 1'b1;
        event_domain_o     = event_index[2:0];
        event_error_code_o = domain_error_code_o[event_index*4 +: 4];
      end
    end

    if (cmd_valid_i && (!command_id_valid || command_busy)) begin
      error_event_o      = 1'b1;
      event_domain_o     = cmd_domain_i;
      event_error_code_o = command_busy ? ERR_BUSY : ERR_ILLEGAL_STATE;
    end
  end
endmodule
