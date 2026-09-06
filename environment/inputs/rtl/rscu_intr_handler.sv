module rscu_intr_handler #(
  parameter int unsigned IRQ_WIDTH = 16
) (
  input  logic                 clk_i,
  input  logic                 reset_n_i,
  input  logic [IRQ_WIDTH-1:0] event_set_i,
  input  logic [IRQ_WIDTH-1:0] clear_i,
  input  logic                 mask_write_i,
  input  logic [IRQ_WIDTH-1:0] mask_wdata_i,
  output logic [IRQ_WIDTH-1:0] raw_o,
  output logic [IRQ_WIDTH-1:0] mask_o,
  output logic [IRQ_WIDTH-1:0] status_o,
  output logic                 irq_o
);
  // REQ_IRQ_002: event set has priority over simultaneous W1C clear.
  always_ff @(posedge clk_i or negedge reset_n_i) begin
    if (!reset_n_i) begin
      raw_o  <= '0;
      mask_o <= '0;
    end else begin
      raw_o <= (raw_o & ~clear_i) | event_set_i;
      if (mask_write_i) begin
        mask_o <= mask_wdata_i;
      end
    end
  end

  always_comb begin
    status_o = raw_o & mask_o;
    irq_o    = |status_o;
  end
endmodule
