module ai_soc_top (
  input  logic pclk,
  input  logic presetn,
  input  logic psel,
  input  logic penable,
  input  logic pwrite,
  input  logic [11:0] paddr,
  input  logic [31:0] pwdata,
  output logic [31:0] prdata,
  output logic pready,
  output logic pslverr,
  input  logic thermal_req_valid_i,
  input  logic [2:0] thermal_req_opp_i,
  input  logic perf_req_valid_i,
  input  logic [2:0] perf_req_opp_i,
  input  logic test_override_i,
  output logic irq_o,
  output logic [7:0] domain_state_o,
  output logic [1:0] current_opp_o,
  output logic [127:0] tile_status_o
);
  localparam int unsigned NUM_DOMAINS = 4;

  logic [3:0] client_idle;
  logic [3:0] client_deny;
  logic [3:0] power_ack;
  logic [3:0] power_good;
  logic [3:0] clock_ack;
  logic [3:0] reset_done;
  logic [3:0] isolation_ack;
  logic [3:0] retention_ack;
  logic [3:0] quiesce_req;
  logic [3:0] power_req;
  logic [3:0] clock_enable;
  logic [3:0] domain_reset_n;
  logic [3:0] isolation;
  logic [3:0] retention;
  logic voltage_req_valid;
  logic [15:0] voltage_code;
  logic voltage_ack;
  logic voltage_stable;
  logic frequency_req_valid;
  logic [15:0] frequency_code;
  logic frequency_ack;
  logic pll_lock;
  logic [15:0] applied_voltage;
  logic [15:0] applied_frequency;
  logic [127:0] gpr_control;
  logic [127:0] gpr_status;
  logic [3:0] tile_busy;
  logic [3:0] tile_done;
  logic any_tile_busy;

  enhanced_rscu_top u_rscu (
    .pclk,
    .presetn,
    .psel,
    .penable,
    .pwrite,
    .paddr,
    .pwdata,
    .prdata,
    .pready,
    .pslverr,
    .client_idle_i          (client_idle),
    .client_deny_i          (client_deny),
    .power_ack_i            (power_ack),
    .power_good_i           (power_good),
    .clock_ack_i            (clock_ack),
    .reset_done_i           (reset_done),
    .isolation_ack_i        (isolation_ack),
    .retention_ack_i        (retention_ack),
    .quiesce_req_o          (quiesce_req),
    .power_req_o            (power_req),
    .clock_enable_o         (clock_enable),
    .reset_n_o              (domain_reset_n),
    .isolation_o            (isolation),
    .retention_o            (retention),
    .thermal_req_valid_i,
    .thermal_req_opp_i,
    .perf_req_valid_i,
    .perf_req_opp_i,
    .voltage_req_valid_o    (voltage_req_valid),
    .voltage_code_o         (voltage_code),
    .voltage_ack_i          (voltage_ack),
    .voltage_stable_i       (voltage_stable),
    .frequency_req_valid_o  (frequency_req_valid),
    .frequency_code_o       (frequency_code),
    .frequency_ack_i        (frequency_ack),
    .pll_lock_i             (pll_lock),
    .gpr_status_i           (gpr_status),
    .gpr_control_o          (gpr_control),
    .test_override_i,
    .irq_o,
    .domain_state_o,
    .current_opp_o
  );

  generate
    genvar tile;
    for (tile = 0; tile < 4; tile = tile + 1) begin : gen_tile
      ai_tile_stub #(
        .TILE_ID (tile)
      ) u_tile (
        .clk_i              (pclk),
        .reset_n_i          (presetn),
        .power_on_i         (power_req[0]),
        .clock_enable_i     (clock_enable[0]),
        .domain_reset_n_i   (domain_reset_n[0]),
        .enable_i           (gpr_control[8+tile]),
        .start_i            (gpr_control[tile]),
        .busy_o             (tile_busy[tile]),
        .done_o             (tile_done[tile]),
        .status_o           (tile_status_o[tile*32 +: 32])
      );
    end
  endgenerate

  always_comb any_tile_busy = |tile_busy;

  generate
    genvar domain;
    for (domain = 0; domain < NUM_DOMAINS; domain = domain + 1) begin : gen_domain_stub
      soc_domain_stub u_domain_stub (
        .clk_i              (pclk),
        .reset_n_i          (presetn),
        .workload_busy_i    ((domain == 0) ? any_tile_busy : 1'b0),
        .quiesce_req_i      (quiesce_req[domain]),
        .power_req_i        (power_req[domain]),
        .clock_enable_i     (clock_enable[domain]),
        .domain_reset_n_i   (domain_reset_n[domain]),
        .isolation_i        (isolation[domain]),
        .retention_i        (retention[domain]),
        .client_idle_o      (client_idle[domain]),
        .client_deny_o      (client_deny[domain]),
        .power_ack_o        (power_ack[domain]),
        .power_good_o       (power_good[domain]),
        .clock_ack_o        (clock_ack[domain]),
        .reset_done_o       (reset_done[domain]),
        .isolation_ack_o    (isolation_ack[domain]),
        .retention_ack_o    (retention_ack[domain])
      );
    end
  endgenerate

  pmic_pll_stub u_pmic_pll_stub (
    .clk_i                  (pclk),
    .reset_n_i              (presetn),
    .voltage_req_valid_i    (voltage_req_valid),
    .voltage_code_i         (voltage_code),
    .frequency_req_valid_i  (frequency_req_valid),
    .frequency_code_i       (frequency_code),
    .voltage_ack_o          (voltage_ack),
    .voltage_stable_o       (voltage_stable),
    .frequency_ack_o        (frequency_ack),
    .pll_lock_o             (pll_lock),
    .applied_voltage_o      (applied_voltage),
    .applied_frequency_o    (applied_frequency)
  );

  always_comb begin
    gpr_status = 128'b0;
    gpr_status[3:0]     = tile_busy;
    gpr_status[7:4]     = tile_done;
    gpr_status[39:32]   = domain_state_o;
    gpr_status[41:40]   = current_opp_o;
    gpr_status[79:64]   = applied_voltage;
    gpr_status[95:80]   = applied_frequency;
  end
endmodule
