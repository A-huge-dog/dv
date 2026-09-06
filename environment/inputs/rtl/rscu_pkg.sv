package rscu_pkg;
  typedef enum logic [1:0] {
    PWR_OFF       = 2'd0,
    PWR_RETENTION = 2'd1,
    PWR_ON        = 2'd2,
    PWR_ERROR     = 2'd3
  } pwr_state_e;

  localparam logic [3:0] ERR_NONE              = 4'd0;
  localparam logic [3:0] ERR_DEPENDENCY        = 4'd1;
  localparam logic [3:0] ERR_ILLEGAL_STATE     = 4'd2;
  localparam logic [3:0] ERR_QUIESCE_DENY      = 4'd3;
  localparam logic [3:0] ERR_QUIESCE_TIMEOUT   = 4'd4;
  localparam logic [3:0] ERR_POWER_TIMEOUT     = 4'd5;
  localparam logic [3:0] ERR_CLOCK_TIMEOUT     = 4'd6;
  localparam logic [3:0] ERR_RESET_TIMEOUT     = 4'd7;
  localparam logic [3:0] ERR_ISOLATION_TIMEOUT = 4'd8;
  localparam logic [3:0] ERR_RETENTION_TIMEOUT = 4'd9;
  localparam logic [3:0] ERR_BUSY              = 4'd10;
  localparam logic [3:0] ERR_ILLEGAL_OPP       = 4'd11;
  localparam logic [3:0] ERR_VOLTAGE_TIMEOUT   = 4'd12;
  localparam logic [3:0] ERR_PLL_TIMEOUT       = 4'd13;
  localparam logic [3:0] ERR_DVFS_UNSAFE       = 4'd14;

  localparam int unsigned IRQ_DOMAIN_DONE    = 0;
  localparam int unsigned IRQ_DOMAIN_ERROR   = 1;
  localparam int unsigned IRQ_DOMAIN_TIMEOUT = 2;
  localparam int unsigned IRQ_QUIESCE_DENY   = 3;
  localparam int unsigned IRQ_DEPENDENCY     = 4;
  localparam int unsigned IRQ_DVFS_DONE      = 5;
  localparam int unsigned IRQ_DVFS_ERROR     = 6;
  localparam int unsigned IRQ_DVFS_TIMEOUT   = 7;
  localparam int unsigned IRQ_ILLEGAL_OPP    = 8;
  localparam int unsigned IRQ_COMMAND_BUSY   = 9;
endpackage
