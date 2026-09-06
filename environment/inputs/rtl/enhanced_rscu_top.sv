module enhanced_rscu_top #(
  parameter int unsigned NUM_DOMAINS = 4,
  parameter int unsigned GPR_WORDS   = 4,
  parameter int unsigned OPP_COUNT   = 4,
  parameter logic [NUM_DOMAINS*NUM_DOMAINS-1:0] DEP_MASK_RESET = 16'h0886
) (
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

  input  logic thermal_req_valid_i,
  input  logic [2:0] thermal_req_opp_i,
  input  logic perf_req_valid_i,
  input  logic [2:0] perf_req_opp_i,
  output logic voltage_req_valid_o,
  output logic [15:0] voltage_code_o,
  input  logic voltage_ack_i,
  input  logic voltage_stable_i,
  output logic frequency_req_valid_o,
  output logic [15:0] frequency_code_o,
  input  logic frequency_ack_i,
  input  logic pll_lock_i,

  input  logic [GPR_WORDS*32-1:0] gpr_status_i,
  output logic [GPR_WORDS*32-1:0] gpr_control_o,
  input  logic test_override_i,
  output logic irq_o,
  output logic [NUM_DOMAINS*2-1:0] domain_state_o,
  output logic [1:0] current_opp_o
);
  import rscu_pkg::*;

  logic csr_wr_en;
  logic csr_rd_en;
  logic [11:0] csr_addr;
  logic [31:0] csr_wdata;
  logic [31:0] csr_rdata;
  logic csr_error;
  logic [15:0] irq_raw;
  logic [15:0] irq_mask;
  logic [15:0] irq_status;
  logic [15:0] irq_clear;
  logic irq_mask_write;
  logic [15:0] irq_mask_wdata;
  logic [15:0] irq_events;
  logic [15:0] error_event_bits;
  logic error_event_valid;
  logic [3:0] error_event_code;
  logic [1:0] error_event_source;
  logic [2:0] error_event_index;
  logic [15:0] error_status;
  logic [15:0] error_info;
  logic [15:0] timeout_cycles;
  logic csr_test_override;
  logic effective_test_override;
  logic global_enable;

  logic gpr_write_valid;
  logic [1:0] gpr_write_index;
  logic [31:0] gpr_write_data;
  logic [GPR_WORDS*32-1:0] gpr_status;

  logic domain_cmd_valid;
  logic [2:0] domain_cmd_id;
  logic [1:0] domain_cmd_target;
  logic [NUM_DOMAINS-1:0] domain_busy;
  logic [NUM_DOMAINS*4-1:0] domain_error_code;
  logic [NUM_DOMAINS*NUM_DOMAINS-1:0] dependency_masks;
  logic domain_done_event;
  logic domain_error_event;
  logic [2:0] domain_event_id;
  logic [3:0] domain_event_error_code;

  logic fw_dvfs_req_valid;
  logic [2:0] fw_dvfs_req_opp;
  logic [OPP_COUNT*32-1:0] opp_table;
  logic opp_write_valid;
  logic [1:0] opp_write_index;
  logic [31:0] opp_write_data;
  logic [1:0] dvfs_target_opp;
  logic dvfs_busy;
  logic dvfs_done_event;
  logic dvfs_error_event;
  logic [3:0] dvfs_event_error_code;
  logic [3:0] dvfs_last_error_q;
  logic dvfs_safe;

  rscu_bridge u_bridge (
    .psel_i       (psel),
    .penable_i    (penable),
    .pwrite_i     (pwrite),
    .paddr_i      (paddr),
    .pwdata_i     (pwdata),
    .prdata_o     (prdata),
    .pready_o     (pready),
    .pslverr_o    (pslverr),
    .csr_wr_en_o  (csr_wr_en),
    .csr_rd_en_o  (csr_rd_en),
    .csr_addr_o   (csr_addr),
    .csr_wdata_o  (csr_wdata),
    .csr_rdata_i  (csr_rdata),
    .csr_error_i  (csr_error)
  );

  rscu_gprb #(
    .GPR_WORDS (GPR_WORDS)
  ) u_gprb (
    .clk_i           (pclk),
    .reset_n_i       (presetn),
    .write_valid_i   (gpr_write_valid),
    .write_index_i   (gpr_write_index),
    .write_data_i    (gpr_write_data),
    .status_i        (gpr_status_i),
    .control_o       (gpr_control_o),
    .status_o        (gpr_status)
  );

  rscu_crpc_plus #(
    .NUM_DOMAINS  (NUM_DOMAINS),
    .DEP_MASK_RESET (DEP_MASK_RESET)
  ) u_crpc_plus (
    .clk_i              (pclk),
    .reset_n_i          (presetn),
    .timeout_cycles_i   (timeout_cycles),
    .test_override_i    (effective_test_override),
    .cmd_valid_i        (domain_cmd_valid),
    .cmd_domain_i       (domain_cmd_id),
    .cmd_target_i       (domain_cmd_target),
    .client_idle_i,
    .client_deny_i,
    .power_ack_i,
    .power_good_i,
    .clock_ack_i,
    .reset_done_i,
    .isolation_ack_i,
    .retention_ack_i,
    .quiesce_req_o,
    .power_req_o,
    .clock_enable_o,
    .reset_n_o,
    .isolation_o,
    .retention_o,
    .domain_state_o,
    .domain_busy_o      (domain_busy),
    .domain_error_code_o(domain_error_code),
    .dependency_masks_o (dependency_masks),
    .done_event_o       (domain_done_event),
    .error_event_o      (domain_error_event),
    .event_domain_o     (domain_event_id),
    .event_error_code_o (domain_event_error_code)
  );

  dvfs_opp_table #(
    .OPP_COUNT (OPP_COUNT)
  ) u_opp_table (
    .clk_i          (pclk),
    .reset_n_i      (presetn),
    .write_valid_i  (opp_write_valid),
    .write_index_i  (opp_write_index),
    .write_data_i   (opp_write_data),
    .idle_i         (!dvfs_busy),
    .opp_table_o    (opp_table)
  );

  always_comb begin
    effective_test_override = test_override_i || csr_test_override;
    dvfs_safe = global_enable && !effective_test_override &&
                (domain_state_o[1:0] == PWR_ON) && !(|domain_busy);
  end

  dvfs_mgr #(
    .OPP_COUNT (OPP_COUNT)
  ) u_dvfs_mgr (
    .clk_i                    (pclk),
    .reset_n_i                (presetn),
    .timeout_cycles_i         (timeout_cycles),
    .safe_to_transition_i     (dvfs_safe),
    .fw_req_valid_i           (fw_dvfs_req_valid),
    .fw_req_opp_i             (fw_dvfs_req_opp),
    .thermal_req_valid_i,
    .thermal_req_opp_i,
    .perf_req_valid_i,
    .perf_req_opp_i,
    .opp_table_i              (opp_table),
    .voltage_ack_i,
    .voltage_stable_i,
    .frequency_ack_i,
    .pll_lock_i,
    .voltage_req_valid_o,
    .voltage_code_o,
    .frequency_req_valid_o,
    .frequency_code_o,
    .current_opp_o,
    .target_opp_o             (dvfs_target_opp),
    .busy_o                   (dvfs_busy),
    .done_event_o             (dvfs_done_event),
    .error_event_o            (dvfs_error_event),
    .error_code_o             (dvfs_event_error_code)
  );

  always_ff @(posedge pclk or negedge presetn) begin
    if (!presetn) begin
      dvfs_last_error_q <= ERR_NONE;
    end else if (dvfs_error_event) begin
      dvfs_last_error_q <= dvfs_event_error_code;
    end
  end

  always_comb begin
    irq_events       = 16'b0;
    error_event_bits = 16'b0;
    if (domain_done_event) begin
      irq_events[IRQ_DOMAIN_DONE] = 1'b1;
    end
    if (domain_error_event) begin
      irq_events[IRQ_DOMAIN_ERROR] = 1'b1;
      error_event_bits[IRQ_DOMAIN_ERROR] = 1'b1;
      if ((domain_event_error_code >= ERR_QUIESCE_TIMEOUT) &&
          (domain_event_error_code <= ERR_RETENTION_TIMEOUT)) begin
        irq_events[IRQ_DOMAIN_TIMEOUT] = 1'b1;
        error_event_bits[IRQ_DOMAIN_TIMEOUT] = 1'b1;
      end
      if (domain_event_error_code == ERR_QUIESCE_DENY) begin
        irq_events[IRQ_QUIESCE_DENY] = 1'b1;
        error_event_bits[IRQ_QUIESCE_DENY] = 1'b1;
      end
      if (domain_event_error_code == ERR_DEPENDENCY) begin
        irq_events[IRQ_DEPENDENCY] = 1'b1;
        error_event_bits[IRQ_DEPENDENCY] = 1'b1;
      end
      if (domain_event_error_code == ERR_BUSY) begin
        irq_events[IRQ_COMMAND_BUSY] = 1'b1;
        error_event_bits[IRQ_COMMAND_BUSY] = 1'b1;
      end
    end
    if (dvfs_done_event) begin
      irq_events[IRQ_DVFS_DONE] = 1'b1;
    end
    if (dvfs_error_event) begin
      irq_events[IRQ_DVFS_ERROR] = 1'b1;
      error_event_bits[IRQ_DVFS_ERROR] = 1'b1;
      if ((dvfs_event_error_code == ERR_VOLTAGE_TIMEOUT) ||
          (dvfs_event_error_code == ERR_PLL_TIMEOUT)) begin
        irq_events[IRQ_DVFS_TIMEOUT] = 1'b1;
        error_event_bits[IRQ_DVFS_TIMEOUT] = 1'b1;
      end
      if (dvfs_event_error_code == ERR_ILLEGAL_OPP) begin
        irq_events[IRQ_ILLEGAL_OPP] = 1'b1;
        error_event_bits[IRQ_ILLEGAL_OPP] = 1'b1;
      end
      if (dvfs_event_error_code == ERR_BUSY) begin
        irq_events[IRQ_COMMAND_BUSY] = 1'b1;
        error_event_bits[IRQ_COMMAND_BUSY] = 1'b1;
      end
    end
  end

  always_comb begin
    error_event_valid  = domain_error_event || dvfs_error_event;
    error_event_code   = dvfs_event_error_code;
    error_event_source = 2'd1;
    error_event_index  = 3'b0;
    if (domain_error_event) begin
      error_event_code   = domain_event_error_code;
      error_event_source = 2'd0;
      error_event_index  = domain_event_id;
    end
  end

  rscu_intr_handler u_intr_handler (
    .clk_i          (pclk),
    .reset_n_i      (presetn),
    .event_set_i    (irq_events),
    .clear_i        (irq_clear),
    .mask_write_i   (irq_mask_write),
    .mask_wdata_i   (irq_mask_wdata),
    .raw_o          (irq_raw),
    .mask_o         (irq_mask),
    .status_o       (irq_status),
    .irq_o
  );

  rscu_csr #(
    .NUM_DOMAINS (NUM_DOMAINS),
    .GPR_WORDS   (GPR_WORDS),
    .OPP_COUNT   (OPP_COUNT)
  ) u_csr (
    .clk_i                    (pclk),
    .reset_n_i                (presetn),
    .wr_en_i                  (csr_wr_en),
    .rd_en_i                  (csr_rd_en),
    .addr_i                   (csr_addr),
    .wdata_i                  (csr_wdata),
    .rdata_o                  (csr_rdata),
    .access_error_o           (csr_error),
    .irq_raw_i                (irq_raw),
    .irq_mask_i               (irq_mask),
    .irq_status_i             (irq_status),
    .irq_clear_o              (irq_clear),
    .irq_mask_write_o         (irq_mask_write),
    .irq_mask_wdata_o         (irq_mask_wdata),
    .error_event_valid_i      (error_event_valid),
    .error_event_bits_i       (error_event_bits),
    .error_event_code_i       (error_event_code),
    .error_event_source_i     (error_event_source),
    .error_event_index_i      (error_event_index),
    .gpr_status_i             (gpr_status),
    .gpr_control_i            (gpr_control_o),
    .gpr_write_valid_o        (gpr_write_valid),
    .gpr_write_index_o        (gpr_write_index),
    .gpr_write_data_o         (gpr_write_data),
    .domain_state_i           (domain_state_o),
    .domain_busy_i            (domain_busy),
    .domain_error_code_i      (domain_error_code),
    .dependency_masks_i       (dependency_masks),
    .domain_cmd_valid_o       (domain_cmd_valid),
    .domain_cmd_id_o          (domain_cmd_id),
    .domain_cmd_target_o      (domain_cmd_target),
    .dvfs_current_opp_i       (current_opp_o),
    .dvfs_target_opp_i        (dvfs_target_opp),
    .dvfs_busy_i              (dvfs_busy),
    .dvfs_error_code_i        (dvfs_last_error_q),
    .fw_dvfs_req_valid_o      (fw_dvfs_req_valid),
    .fw_dvfs_req_opp_o        (fw_dvfs_req_opp),
    .opp_table_i              (opp_table),
    .opp_write_valid_o        (opp_write_valid),
    .opp_write_index_o        (opp_write_index),
    .opp_write_data_o         (opp_write_data),
    .timeout_cycles_o         (timeout_cycles),
    .test_override_o          (csr_test_override),
    .global_enable_o          (global_enable),
    .error_status_o           (error_status),
    .error_info_o             (error_info)
  );
endmodule
