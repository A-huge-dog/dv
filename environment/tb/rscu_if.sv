interface rscu_if(input logic pclk);
  logic presetn;
  logic psel;
  logic penable;
  logic pwrite;
  logic [11:0] paddr;
  logic [31:0] pwdata;
  logic [31:0] prdata;
  logic pready;
  logic pslverr;

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

  logic thermal_req_valid;
  logic [2:0] thermal_req_opp;
  logic perf_req_valid;
  logic [2:0] perf_req_opp;
  logic voltage_req_valid;
  logic [15:0] voltage_code;
  logic voltage_ack;
  logic voltage_stable;
  logic frequency_req_valid;
  logic [15:0] frequency_code;
  logic frequency_ack;
  logic pll_lock;

  logic [127:0] gpr_status;
  logic [127:0] gpr_control;
  logic test_override;
  logic irq;
  logic [7:0] domain_state;
  logic [1:0] current_opp;

  // Testbench-only mock controls. A set hold bit freezes its response.
  logic [3:0] mock_client_idle;
  logic [3:0] mock_client_deny;
  logic [3:0] hold_power;
  logic [3:0] hold_clock;
  logic [3:0] hold_reset;
  logic [3:0] hold_isolation;
  logic [3:0] hold_retention;
  logic hold_voltage;
  logic hold_frequency;
endinterface
