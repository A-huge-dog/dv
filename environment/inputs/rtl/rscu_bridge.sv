module rscu_bridge (
  input  logic        psel_i,
  input  logic        penable_i,
  input  logic        pwrite_i,
  input  logic [11:0] paddr_i,
  input  logic [31:0] pwdata_i,
  output logic [31:0] prdata_o,
  output logic        pready_o,
  output logic        pslverr_o,

  output logic        csr_wr_en_o,
  output logic        csr_rd_en_o,
  output logic [11:0] csr_addr_o,
  output logic [31:0] csr_wdata_o,
  input  logic [31:0] csr_rdata_i,
  input  logic        csr_error_i
);
  logic access;

  always_comb begin
    access       = psel_i && penable_i;
    csr_wr_en_o  = access && pwrite_i;
    csr_rd_en_o  = access && !pwrite_i;
    csr_addr_o   = paddr_i;
    csr_wdata_o  = pwdata_i;
    prdata_o     = csr_rdata_i;
    pready_o     = 1'b1;
    pslverr_o    = access && ((paddr_i[1:0] != 2'b00) || csr_error_i);
  end
endmodule
