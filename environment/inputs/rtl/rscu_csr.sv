module rscu_csr #(
  parameter int unsigned NUM_DOMAINS = 4,
  parameter int unsigned GPR_WORDS   = 4,
  parameter int unsigned OPP_COUNT   = 4
) (
  input  logic clk_i,
  input  logic reset_n_i,
  input  logic wr_en_i,
  input  logic rd_en_i,
  input  logic [11:0] addr_i,
  input  logic [31:0] wdata_i,
  output logic [31:0] rdata_o,
  output logic access_error_o,

  input  logic [15:0] irq_raw_i,
  input  logic [15:0] irq_mask_i,
  input  logic [15:0] irq_status_i,
  output logic [15:0] irq_clear_o,
  output logic irq_mask_write_o,
  output logic [15:0] irq_mask_wdata_o,

  input  logic error_event_valid_i,
  input  logic [15:0] error_event_bits_i,
  input  logic [3:0] error_event_code_i,
  input  logic [1:0] error_event_source_i,
  input  logic [2:0] error_event_index_i,

  input  logic [GPR_WORDS*32-1:0] gpr_status_i,
  input  logic [GPR_WORDS*32-1:0] gpr_control_i,
  output logic gpr_write_valid_o,
  output logic [1:0] gpr_write_index_o,
  output logic [31:0] gpr_write_data_o,

  input  logic [NUM_DOMAINS*2-1:0] domain_state_i,
  input  logic [NUM_DOMAINS-1:0] domain_busy_i,
  input  logic [NUM_DOMAINS*4-1:0] domain_error_code_i,
  input  logic [NUM_DOMAINS*NUM_DOMAINS-1:0] dependency_masks_i,
  output logic domain_cmd_valid_o,
  output logic [2:0] domain_cmd_id_o,
  output logic [1:0] domain_cmd_target_o,

  input  logic [1:0] dvfs_current_opp_i,
  input  logic [1:0] dvfs_target_opp_i,
  input  logic dvfs_busy_i,
  input  logic [3:0] dvfs_error_code_i,
  output logic fw_dvfs_req_valid_o,
  output logic [2:0] fw_dvfs_req_opp_o,
  input  logic [OPP_COUNT*32-1:0] opp_table_i,
  output logic opp_write_valid_o,
  output logic [1:0] opp_write_index_o,
  output logic [31:0] opp_write_data_o,

  output logic [15:0] timeout_cycles_o,
  output logic test_override_o,
  output logic global_enable_o,
  output logic [15:0] error_status_o,
  output logic [15:0] error_info_o
);
  localparam logic [11:0] ADDR_ID              = 12'h000;
  localparam logic [11:0] ADDR_VERSION         = 12'h004;
  localparam logic [11:0] ADDR_CAPABILITY      = 12'h008;
  localparam logic [11:0] ADDR_GLOBAL_CTRL     = 12'h00c;
  localparam logic [11:0] ADDR_IRQ_RAW         = 12'h010;
  localparam logic [11:0] ADDR_IRQ_MASK        = 12'h014;
  localparam logic [11:0] ADDR_IRQ_STATUS      = 12'h018;
  localparam logic [11:0] ADDR_IRQ_CLEAR       = 12'h01c;
  localparam logic [11:0] ADDR_ERROR_STATUS    = 12'h020;
  localparam logic [11:0] ADDR_ERROR_INFO      = 12'h024;
  localparam logic [11:0] ADDR_TIMEOUT_CFG     = 12'h028;
  localparam logic [11:0] ADDR_DEBUG_OVERRIDE  = 12'h02c;
  localparam logic [11:0] ADDR_GPR_CTRL0       = 12'h040;
  localparam logic [11:0] ADDR_GPR_CTRL1       = 12'h044;
  localparam logic [11:0] ADDR_GPR_CTRL2       = 12'h048;
  localparam logic [11:0] ADDR_GPR_CTRL3       = 12'h04c;
  localparam logic [11:0] ADDR_GPR_STATUS0     = 12'h060;
  localparam logic [11:0] ADDR_GPR_STATUS1     = 12'h064;
  localparam logic [11:0] ADDR_GPR_STATUS2     = 12'h068;
  localparam logic [11:0] ADDR_GPR_STATUS3     = 12'h06c;
  localparam logic [11:0] ADDR_DOMAIN_CMD      = 12'h080;
  localparam logic [11:0] ADDR_DOMAIN_STATUS   = 12'h084;
  localparam logic [11:0] ADDR_DOMAIN_STATES   = 12'h088;
  localparam logic [11:0] ADDR_DOMAIN_BUSY     = 12'h08c;
  localparam logic [11:0] ADDR_DEP_MASK0       = 12'h0a0;
  localparam logic [11:0] ADDR_DEP_MASK1       = 12'h0a4;
  localparam logic [11:0] ADDR_DEP_MASK2       = 12'h0a8;
  localparam logic [11:0] ADDR_DEP_MASK3       = 12'h0ac;
  localparam logic [11:0] ADDR_DVFS_CMD        = 12'h100;
  localparam logic [11:0] ADDR_DVFS_STATUS     = 12'h104;
  localparam logic [11:0] ADDR_OPP0            = 12'h110;
  localparam logic [11:0] ADDR_OPP1            = 12'h114;
  localparam logic [11:0] ADDR_OPP2            = 12'h118;
  localparam logic [11:0] ADDR_OPP3            = 12'h11c;

  logic [2:0] last_domain_id_q;
  logic valid_read;
  logic valid_write;
  logic [31:0] selected_domain_status;
  logic [15:0] error_clear;

  function automatic logic [31:0] dependency_mask_row(
    input int unsigned row
  );
    logic [31:0] result;
    begin
      result = 32'b0;
      if (row < NUM_DOMAINS) begin
        result[NUM_DOMAINS-1:0] =
          dependency_masks_i[(row*NUM_DOMAINS) +: NUM_DOMAINS];
      end
      return result;
    end
  endfunction

  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      global_enable_o  <= 1'b1;
      timeout_cycles_o <= 16'd32;
      test_override_o  <= 1'b0;
      error_status_o   <= 16'b0;
      error_info_o     <= 16'b0;
      last_domain_id_q <= 3'b0;
    end else begin
      if (wr_en_i && (addr_i == ADDR_GLOBAL_CTRL)) begin
        global_enable_o <= wdata_i[0];
      end
      if (wr_en_i && (addr_i == ADDR_TIMEOUT_CFG)) begin
        timeout_cycles_o <= wdata_i[15:0];
      end
      if (wr_en_i && (addr_i == ADDR_DEBUG_OVERRIDE)) begin
        test_override_o <= wdata_i[0];
      end
      if (wr_en_i && (addr_i == ADDR_DOMAIN_CMD) && wdata_i[8]) begin
        last_domain_id_q <= wdata_i[2:0];
      end

      error_status_o <= (error_status_o & ~error_clear) | error_event_bits_i;
      if (error_event_valid_i) begin
        error_info_o <= {6'b0, error_event_source_i,
                         1'b0, error_event_index_i, error_event_code_i};
      end
    end
  end

  always_comb begin
    irq_clear_o       = 16'b0;
    irq_mask_write_o  = 1'b0;
    irq_mask_wdata_o  = wdata_i[15:0];
    error_clear       = 16'b0;
    gpr_write_valid_o = 1'b0;
    gpr_write_index_o = 2'b0;
    gpr_write_data_o  = wdata_i;
    domain_cmd_valid_o  = 1'b0;
    domain_cmd_id_o     = wdata_i[2:0];
    domain_cmd_target_o = wdata_i[5:4];
    fw_dvfs_req_valid_o = 1'b0;
    fw_dvfs_req_opp_o   = wdata_i[2:0];
    opp_write_valid_o = 1'b0;
    opp_write_index_o = 2'b0;
    opp_write_data_o  = wdata_i;

    if (wr_en_i) begin
      case (addr_i)
        ADDR_IRQ_MASK: begin
          irq_mask_write_o = 1'b1;
        end
        ADDR_IRQ_CLEAR: begin
          irq_clear_o = wdata_i[15:0];
        end
        ADDR_ERROR_STATUS: begin
          error_clear = wdata_i[15:0];
        end
        ADDR_GPR_CTRL0, ADDR_GPR_CTRL1,
        ADDR_GPR_CTRL2, ADDR_GPR_CTRL3: begin
          gpr_write_valid_o = 1'b1;
          gpr_write_index_o = addr_i[3:2];
        end
        ADDR_DOMAIN_CMD: begin
          domain_cmd_valid_o = wdata_i[8] && global_enable_o;
        end
        ADDR_DVFS_CMD: begin
          fw_dvfs_req_valid_o = wdata_i[8] && global_enable_o;
        end
        ADDR_OPP0, ADDR_OPP1, ADDR_OPP2, ADDR_OPP3: begin
          opp_write_valid_o = !dvfs_busy_i;
          opp_write_index_o = addr_i[3:2];
        end
        default: begin
        end
      endcase
    end
  end

  always_comb begin
    selected_domain_status = 32'b0;
    if (last_domain_id_q < NUM_DOMAINS) begin
      selected_domain_status[1:0] =
        domain_state_i[last_domain_id_q*2 +: 2];
      selected_domain_status[2] = domain_busy_i[last_domain_id_q];
      selected_domain_status[6:3] =
        domain_error_code_i[last_domain_id_q*4 +: 4];
      selected_domain_status[10:8] = last_domain_id_q;
    end
  end

  always_comb begin
    valid_read = 1'b1;
    rdata_o    = 32'b0;
    case (addr_i)
      ADDR_ID:             rdata_o = 32'h5253_4355;
      ADDR_VERSION:        rdata_o = 32'h0001_0000;
      ADDR_CAPABILITY:     rdata_o = 32'h0404_0401;
      ADDR_GLOBAL_CTRL:    rdata_o = {31'b0, global_enable_o};
      ADDR_IRQ_RAW:        rdata_o = {16'b0, irq_raw_i};
      ADDR_IRQ_MASK:       rdata_o = {16'b0, irq_mask_i};
      ADDR_IRQ_STATUS:     rdata_o = {16'b0, irq_status_i};
      ADDR_IRQ_CLEAR:      rdata_o = 32'b0;
      ADDR_ERROR_STATUS:   rdata_o = {16'b0, error_status_o};
      ADDR_ERROR_INFO:     rdata_o = {16'b0, error_info_o};
      ADDR_TIMEOUT_CFG:    rdata_o = {16'b0, timeout_cycles_o};
      ADDR_DEBUG_OVERRIDE: rdata_o = {31'b0, test_override_o};
      ADDR_GPR_CTRL0: rdata_o = gpr_control_i[0*32 +: 32];
      ADDR_GPR_CTRL1: rdata_o = gpr_control_i[1*32 +: 32];
      ADDR_GPR_CTRL2: rdata_o = gpr_control_i[2*32 +: 32];
      ADDR_GPR_CTRL3: rdata_o = gpr_control_i[3*32 +: 32];
      ADDR_GPR_STATUS0: rdata_o = gpr_status_i[0*32 +: 32];
      ADDR_GPR_STATUS1: rdata_o = gpr_status_i[1*32 +: 32];
      ADDR_GPR_STATUS2: rdata_o = gpr_status_i[2*32 +: 32];
      ADDR_GPR_STATUS3: rdata_o = gpr_status_i[3*32 +: 32];
      ADDR_DOMAIN_CMD:    rdata_o = 32'b0;
      ADDR_DOMAIN_STATUS: rdata_o = selected_domain_status;
      ADDR_DOMAIN_STATES: begin
        rdata_o = 32'b0;
        rdata_o[NUM_DOMAINS*2-1:0] = domain_state_i;
      end
      ADDR_DOMAIN_BUSY: begin
        rdata_o = 32'b0;
        rdata_o[NUM_DOMAINS-1:0] = domain_busy_i;
      end
      ADDR_DEP_MASK0: rdata_o = dependency_mask_row(0);
      ADDR_DEP_MASK1: rdata_o = dependency_mask_row(1);
      ADDR_DEP_MASK2: rdata_o = dependency_mask_row(2);
      ADDR_DEP_MASK3: rdata_o = dependency_mask_row(3);
      ADDR_DVFS_CMD: rdata_o = 32'b0;
      ADDR_DVFS_STATUS: begin
        rdata_o = 32'b0;
        rdata_o[1:0]   = dvfs_current_opp_i;
        rdata_o[5:4]   = dvfs_target_opp_i;
        rdata_o[8]     = dvfs_busy_i;
        rdata_o[15:12] = dvfs_error_code_i;
      end
      ADDR_OPP0: rdata_o = opp_table_i[0*32 +: 32];
      ADDR_OPP1: rdata_o = opp_table_i[1*32 +: 32];
      ADDR_OPP2: rdata_o = opp_table_i[2*32 +: 32];
      ADDR_OPP3: rdata_o = opp_table_i[3*32 +: 32];
      default: begin
        rdata_o    = 32'b0;
        valid_read = 1'b0;
      end
    endcase
  end

  always_comb begin
    valid_write = 1'b1;
    case (addr_i)
      ADDR_GLOBAL_CTRL,
      ADDR_IRQ_MASK,
      ADDR_IRQ_CLEAR,
      ADDR_ERROR_STATUS,
      ADDR_TIMEOUT_CFG,
      ADDR_DEBUG_OVERRIDE,
      ADDR_GPR_CTRL0, ADDR_GPR_CTRL1,
      ADDR_GPR_CTRL2, ADDR_GPR_CTRL3,
      ADDR_DOMAIN_CMD,
      ADDR_DVFS_CMD: begin
      end
      ADDR_OPP0, ADDR_OPP1, ADDR_OPP2, ADDR_OPP3: begin
        valid_write = !dvfs_busy_i;
      end
      default: valid_write = 1'b0;
    endcase

    access_error_o = (rd_en_i && !valid_read) ||
                     (wr_en_i && (!valid_write ||
                      (((addr_i == ADDR_DOMAIN_CMD) ||
                        (addr_i == ADDR_DVFS_CMD)) && wdata_i[8] &&
                       !global_enable_o)));
  end
endmodule
