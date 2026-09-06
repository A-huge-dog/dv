package rscu_uvm_pkg;
  import uvm_pkg::*;
  import rscu_ral_pkg::*;
  `include "uvm_macros.svh"

  class rscu_apb_item extends uvm_sequence_item;
    rand bit write;
    rand bit [11:0] addr;
    rand bit [31:0] data;
    bit [31:0] rdata;
    bit slverr;

    `uvm_object_utils_begin(rscu_apb_item)
      `uvm_field_int(write, UVM_DEFAULT)
      `uvm_field_int(addr, UVM_DEFAULT)
      `uvm_field_int(data, UVM_DEFAULT)
      `uvm_field_int(rdata, UVM_DEFAULT | UVM_NOPACK)
      `uvm_field_int(slverr, UVM_DEFAULT | UVM_NOPACK)
    `uvm_object_utils_end

    function new(string name = "rscu_apb_item");
      super.new(name);
    endfunction
  endclass

  class rscu_apb_sequence extends uvm_sequence #(rscu_apb_item);
    `uvm_object_utils(rscu_apb_sequence)
    bit write;
    bit [11:0] addr;
    bit [31:0] data;
    bit [31:0] rdata;
    bit slverr;

    function new(string name = "rscu_apb_sequence");
      super.new(name);
    endfunction

    virtual task body();
      rscu_apb_item request;
      request = rscu_apb_item::type_id::create("request");
      start_item(request);
      request.write = write;
      request.addr  = addr;
      request.data  = data;
      finish_item(request);
      rdata  = request.rdata;
      slverr = request.slverr;
    endtask
  endclass

  class rscu_apb_sequencer extends uvm_sequencer #(rscu_apb_item);
    `uvm_component_utils(rscu_apb_sequencer)
    function new(string name, uvm_component parent);
      super.new(name, parent);
    endfunction
  endclass

  class rscu_apb_driver extends uvm_driver #(rscu_apb_item);
    `uvm_component_utils(rscu_apb_driver)
    virtual rscu_if vif;

    function new(string name, uvm_component parent);
      super.new(name, parent);
    endfunction

    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      if (!uvm_config_db#(virtual rscu_if)::get(this, "", "vif", vif))
        `uvm_fatal("NOVIF", "rscu_apb_driver did not receive rscu_if")
    endfunction

    virtual task run_phase(uvm_phase phase);
      rscu_apb_item request;
      vif.psel    <= 1'b0;
      vif.penable <= 1'b0;
      vif.pwrite  <= 1'b0;
      vif.paddr   <= 12'b0;
      vif.pwdata  <= 32'b0;
      forever begin
        seq_item_port.get_next_item(request);
        @(negedge vif.pclk);
        vif.psel    <= 1'b1;
        vif.penable <= 1'b0;
        vif.pwrite  <= request.write;
        vif.paddr   <= request.addr;
        vif.pwdata  <= request.data;
        @(negedge vif.pclk);
        vif.penable <= 1'b1;
        @(negedge vif.pclk);
        request.rdata  = vif.prdata;
        request.slverr = vif.pslverr;
        vif.psel       <= 1'b0;
        vif.penable    <= 1'b0;
        vif.pwrite     <= 1'b0;
        seq_item_port.item_done();
      end
    endtask
  endclass

  class rscu_apb_monitor extends uvm_monitor;
    `uvm_component_utils(rscu_apb_monitor)
    virtual rscu_if vif;
    uvm_analysis_port #(rscu_apb_item) analysis_port;

    function new(string name, uvm_component parent);
      super.new(name, parent);
      analysis_port = new("analysis_port", this);
    endfunction

    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      if (!uvm_config_db#(virtual rscu_if)::get(this, "", "vif", vif))
        `uvm_fatal("NOVIF", "rscu_apb_monitor did not receive rscu_if")
    endfunction

    virtual task run_phase(uvm_phase phase);
      rscu_apb_item observed;
      forever begin
        @(posedge vif.pclk);
        if (vif.presetn && vif.psel && vif.penable) begin
          observed = rscu_apb_item::type_id::create("observed");
          observed.write  = vif.pwrite;
          observed.addr   = vif.paddr;
          observed.data   = vif.pwdata;
          observed.rdata  = vif.prdata;
          observed.slverr = vif.pslverr;
          analysis_port.write(observed);
        end
      end
    endtask
  endclass

  class rscu_scoreboard extends uvm_scoreboard;
    `uvm_component_utils(rscu_scoreboard)
    uvm_analysis_imp #(rscu_apb_item, rscu_scoreboard) analysis_export;
    int unsigned transaction_count;

    function new(string name, uvm_component parent);
      super.new(name, parent);
      analysis_export = new("analysis_export", this);
    endfunction

    function void write(rscu_apb_item item);
      transaction_count++;
      if (^item.rdata === 1'bx)
        `uvm_error("APB_X", $sformatf("APB response contains X at 0x%03h", item.addr))
    endfunction
  endclass

  class rscu_apb_agent extends uvm_agent;
    `uvm_component_utils(rscu_apb_agent)
    rscu_apb_sequencer sequencer;
    rscu_apb_driver driver;
    rscu_apb_monitor monitor;

    function new(string name, uvm_component parent);
      super.new(name, parent);
    endfunction

    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      sequencer = rscu_apb_sequencer::type_id::create("sequencer", this);
      driver    = rscu_apb_driver::type_id::create("driver", this);
      monitor   = rscu_apb_monitor::type_id::create("monitor", this);
    endfunction

    virtual function void connect_phase(uvm_phase phase);
      super.connect_phase(phase);
      driver.seq_item_port.connect(sequencer.seq_item_export);
    endfunction
  endclass

  class rscu_env extends uvm_env;
    `uvm_component_utils(rscu_env)
    rscu_apb_agent apb;
    rscu_scoreboard scoreboard;
    rscu_reg_block ral;

    function new(string name, uvm_component parent);
      super.new(name, parent);
    endfunction

    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      apb = rscu_apb_agent::type_id::create("apb", this);
      scoreboard = rscu_scoreboard::type_id::create("scoreboard", this);
      ral = rscu_reg_block::type_id::create("ral");
      ral.build();
    endfunction

    virtual function void connect_phase(uvm_phase phase);
      super.connect_phase(phase);
      apb.monitor.analysis_port.connect(scoreboard.analysis_export);
    endfunction
  endclass

  class rscu_base_test extends uvm_test;
    `uvm_component_utils(rscu_base_test)
    rscu_env env;
    virtual rscu_if vif;

    function new(string name, uvm_component parent);
      super.new(name, parent);
    endfunction

    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      env = rscu_env::type_id::create("env", this);
      if (!uvm_config_db#(virtual rscu_if)::get(this, "", "vif", vif))
        `uvm_fatal("NOVIF", "rscu_base_test did not receive rscu_if")
    endfunction

    task automatic reset_dut(
      bit [3:0] hold_power = 0,
      bit [3:0] hold_clock = 0,
      bit [3:0] hold_reset = 0,
      bit [3:0] hold_isolation = 0,
      bit [3:0] hold_retention = 0,
      bit hold_voltage = 0,
      bit hold_frequency = 0
    );
      vif.presetn          = 1'b0;
      vif.mock_client_idle = 4'hf;
      vif.mock_client_deny = 4'h0;
      vif.hold_power       = hold_power;
      vif.hold_clock       = hold_clock;
      vif.hold_reset       = hold_reset;
      vif.hold_isolation   = hold_isolation;
      vif.hold_retention   = hold_retention;
      vif.hold_voltage     = hold_voltage;
      vif.hold_frequency   = hold_frequency;
      vif.thermal_req_valid = 1'b0;
      vif.thermal_req_opp   = 3'b0;
      vif.perf_req_valid    = 1'b0;
      vif.perf_req_opp      = 3'b0;
      vif.gpr_status        = 128'h44444444_33333333_22222222_11111111;
      vif.test_override     = 1'b0;
      repeat (4) @(posedge vif.pclk);
      vif.presetn = 1'b1;
      repeat (3) @(posedge vif.pclk);
    endtask

    task automatic apb_transfer(
      input bit write,
      input bit [11:0] addr,
      input bit [31:0] data,
      output bit [31:0] rdata,
      output bit slverr
    );
      rscu_apb_sequence sequence_h;
      sequence_h = rscu_apb_sequence::type_id::create("sequence_h");
      sequence_h.write = write;
      sequence_h.addr  = addr;
      sequence_h.data  = data;
      sequence_h.start(env.apb.sequencer);
      rdata  = sequence_h.rdata;
      slverr = sequence_h.slverr;
    endtask

    task automatic apb_write(input bit [11:0] addr, input bit [31:0] data);
      bit [31:0] ignored;
      bit slverr;
      apb_transfer(1'b1, addr, data, ignored, slverr);
      if (slverr)
        `uvm_error("APB_WRITE", $sformatf("Unexpected PSLVERR at 0x%03h", addr))
    endtask

    task automatic apb_read(input bit [11:0] addr, output bit [31:0] data);
      bit slverr;
      apb_transfer(1'b0, addr, 32'b0, data, slverr);
      if (slverr)
        `uvm_error("APB_READ", $sformatf("Unexpected PSLVERR at 0x%03h", addr))
    endtask

    task automatic expect_equal(
      input bit [31:0] actual,
      input bit [31:0] expected,
      input string label
    );
      if (actual !== expected)
        `uvm_error("MISMATCH", $sformatf("%s expected 0x%08h got 0x%08h",
                                        label, expected, actual))
    endtask

    task automatic wait_domain_state(input int unsigned id, input bit [1:0] expected);
      int unsigned count;
      bit found;
      found = 1'b0;
      for (count = 0; count < 100; count++) begin
        @(posedge vif.pclk);
        if (vif.domain_state[id*2 +: 2] == expected) begin
          found = 1'b1;
          break;
        end
      end
      if (!found)
        `uvm_error("DOMAIN_TIMEOUT", $sformatf("domain %0d did not reach state %0d", id, expected))
    endtask

    task automatic wait_current_opp(input bit [1:0] expected);
      int unsigned count;
      bit found;
      found = 1'b0;
      for (count = 0; count < 100; count++) begin
        @(posedge vif.pclk);
        if (vif.current_opp == expected) begin
          found = 1'b1;
          break;
        end
      end
      if (!found)
        `uvm_error("DVFS_TIMEOUT", $sformatf("current OPP did not reach %0d", expected))
    endtask

    task automatic issue_power(input bit [2:0] id, input bit [1:0] target);
      apb_write(12'h080, 32'h00000100 | (target << 4) | id);
    endtask

    task automatic bring_compute_on();
      issue_power(3, 2); wait_domain_state(3, 2);
      issue_power(1, 2); wait_domain_state(1, 2);
      issue_power(2, 2); wait_domain_state(2, 2);
      issue_power(0, 2); wait_domain_state(0, 2);
    endtask

    task automatic check_domain_error(input bit [2:0] id, input bit [3:0] code);
      bit [31:0] status;
      apb_read(12'h084, status);
      if ((status[10:8] != id) || (status[6:3] != code))
        `uvm_error("DOMAIN_ERROR", $sformatf("domain %0d expected error %0d, status=0x%08h",
                                            id, code, status))
    endtask

    task automatic run_reset_csr();
      bit [31:0] value;
      bit [31:0] ignored;
      bit slverr;
      reset_dut();
      apb_read(12'h000, value); expect_equal(value, 32'h52534355, "ID");
      apb_read(12'h004, value); expect_equal(value, 32'h00010000, "VERSION");
      apb_read(12'h008, value); expect_equal(value, 32'h04040401, "CAPABILITY");
      apb_read(12'h00c, value); expect_equal(value, 32'h00000001, "GLOBAL_CTRL");
      apb_read(12'h010, value); expect_equal(value, 32'h00000000, "IRQ_RAW reset");
      apb_read(12'h014, value); expect_equal(value, 32'h00000000, "IRQ_MASK reset");
      apb_read(12'h020, value); expect_equal(value, 32'h00000000, "ERROR_STATUS reset");
      apb_read(12'h028, value); expect_equal(value, 32'h00000020, "TIMEOUT_CFG");
      apb_read(12'h110, value); expect_equal(value, 32'h01900320, "OPP0");
      apb_write(12'h040, 32'ha5a55a5a);
      apb_read(12'h040, value); expect_equal(value, 32'ha5a55a5a, "GPR_CTRL0");
      apb_read(12'h060, value); expect_equal(value, 32'h11111111, "GPR_STATUS0");
      apb_write(12'h014, 32'h000003ff);
      apb_read(12'h014, value); expect_equal(value, 32'h000003ff, "IRQ_MASK RW");
      apb_write(12'h028, 32'hffff1234);
      apb_read(12'h028, value); expect_equal(value, 32'h00001234, "TIMEOUT low 16 bits");
      apb_write(12'h110, 32'h02bc0366);
      apb_read(12'h110, value); expect_equal(value, 32'h02bc0366, "OPP0 idle write");

      apb_transfer(1'b1, 12'h000, 32'h0, ignored, slverr);
      if (!slverr) `uvm_error("APB_RO", "write to ID did not assert PSLVERR")
      apb_read(12'h000, value); expect_equal(value, 32'h52534355, "ID after RO write");
      apb_transfer(1'b0, 12'h001, 0, ignored, slverr);
      if (!slverr) `uvm_error("APB_ALIGN", "misaligned address did not assert PSLVERR")
      apb_transfer(1'b0, 12'h3fc, 0, ignored, slverr);
      if (!slverr) `uvm_error("APB_ERROR", "invalid address did not assert PSLVERR")

      apb_write(12'h00c, 0);
      apb_transfer(1'b1, 12'h080, 32'h00000123, ignored, slverr);
      if (!slverr) `uvm_error("GLOBAL_DISABLE", "disabled power START did not assert PSLVERR")
      apb_transfer(1'b1, 12'h100, 32'h00000101, ignored, slverr);
      if (!slverr) `uvm_error("GLOBAL_DISABLE", "disabled DVFS START did not assert PSLVERR")
      apb_read(12'h088, value); expect_equal(value, 0, "disabled command state");
    endtask

    task automatic run_csr_irq_side_effects();
      bit [31:0] value;
      reset_dut();

      // Mask reset keeps the external IRQ low even when a raw event is sticky.
      issue_power(0, 2);
      repeat (4) @(posedge vif.pclk);
      apb_read(12'h010, value);
      if (!value[4] || vif.irq)
        `uvm_error("IRQ_MASK", "raw dependency event or reset mask behavior is wrong")
      apb_write(12'h014, 32'h00000010);
      repeat (2) @(posedge vif.pclk);
      if (!vif.irq) `uvm_error("IRQ_MASK", "masked dependency event did not assert irq")

      apb_read(12'h020, value);
      if (!value[1] || !value[4])
        `uvm_error("ERROR_STATUS", "dependency error bits were not sticky")
      apb_write(12'h020, 32'h00000012);
      apb_read(12'h020, value); expect_equal(value, 0, "ERROR_STATUS W1C");
      apb_write(12'h01c, 32'h00000010);
      apb_read(12'h010, value);
      if (value[4] || vif.irq)
        `uvm_error("IRQ_CLEAR", "IRQ W1C did not clear raw/status/irq")
    endtask

    task automatic run_power_normal();
      bit [31:0] value;
      reset_dut();
      apb_write(12'h014, 32'h0000ffff);
      bring_compute_on();
      issue_power(0, 1); wait_domain_state(0, 1);
      issue_power(0, 2); wait_domain_state(0, 2);
      issue_power(0, 0); wait_domain_state(0, 0);
      issue_power(1, 0); wait_domain_state(1, 0);
      issue_power(2, 0); wait_domain_state(2, 0);
      issue_power(3, 0); wait_domain_state(3, 0);
      apb_read(12'h010, value);
      if (!value[0]) `uvm_error("IRQ", "domain done event was not sticky")
      apb_write(12'h01c, 32'h0000ffff);
      apb_read(12'h010, value);
      expect_equal(value, 0, "IRQ_RAW after W1C");
    endtask

    task automatic run_dependency_and_deny();
      bit [31:0] value;
      reset_dut();
      issue_power(0, 2);
      repeat (4) @(posedge vif.pclk);
      if (vif.domain_state[1:0] != 0)
        `uvm_error("DEPENDENCY", "illegal compute power-on changed state")
      check_domain_error(0, 1);
      apb_read(12'h010, value);
      if (!value[4]) `uvm_error("DEPENDENCY", "dependency IRQ bit not set")

      reset_dut();
      bring_compute_on();
      vif.mock_client_idle[0] = 1'b0;
      vif.mock_client_deny[0] = 1'b1;
      issue_power(0, 0);
      repeat (8) @(posedge vif.pclk);
      if (vif.domain_state[1:0] != 2)
        `uvm_error("QUIESCE_DENY", "deny did not preserve ON state")
      check_domain_error(0, 3);
      apb_read(12'h010, value);
      if (!value[3]) `uvm_error("QUIESCE_DENY", "deny IRQ bit not set")
    endtask

    task automatic run_power_edge_cases();
      bit [31:0] value;
      reset_dut();

      // No-op OFF request completes without changing safe controls.
      issue_power(3, 0);
      repeat (3) @(posedge vif.pclk);
      if ((vif.domain_state[7:6] != 0) || vif.power_req[3] ||
          vif.clock_enable[3] || vif.domain_reset_n[3] ||
          !vif.isolation[3] || vif.retention[3])
        `uvm_error("POWER_NOOP", "OFF no-op changed state or controls")
      apb_read(12'h010, value);
      if (!value[0]) `uvm_error("POWER_NOOP", "OFF no-op did not emit done")

      issue_power(3, 1);
      repeat (3) @(posedge vif.pclk);
      if (vif.domain_state[7:6] != 0)
        `uvm_error("ILLEGAL_STATE", "OFF-to-RETENTION changed state")
      check_domain_error(3, 2);

      issue_power(3, 3);
      repeat (3) @(posedge vif.pclk);
      check_domain_error(3, 2);

      // A second command while the first waits must report BUSY and not replace it.
      reset_dut(4'b1000);
      apb_write(12'h028, 0);
      issue_power(3, 2);
      repeat (3) @(posedge vif.pclk);
      issue_power(3, 0);
      repeat (3) @(posedge vif.pclk);
      apb_read(12'h024, value);
      if (value[3:0] != 10)
        `uvm_error("POWER_BUSY", "busy command did not record ERR_BUSY")
      apb_read(12'h010, value);
      if (!value[9]) `uvm_error("POWER_BUSY", "busy command IRQ was not sticky")
    endtask

    task automatic run_power_timeouts();
      // POWER timeout
      reset_dut(4'b1000);
      apb_write(12'h028, 3);
      issue_power(3, 2); wait_domain_state(3, 3); check_domain_error(3, 5);
      vif.hold_power[3] = 1'b0;
      issue_power(3, 0); wait_domain_state(3, 0);

      // CLOCK timeout
      reset_dut(0, 4'b1000);
      apb_write(12'h028, 3);
      issue_power(3, 2); wait_domain_state(3, 3); check_domain_error(3, 6);

      // RESET timeout
      reset_dut(0, 0, 4'b1000);
      apb_write(12'h028, 3);
      issue_power(3, 2); wait_domain_state(3, 3); check_domain_error(3, 7);

      // ISOLATION timeout
      reset_dut(0, 0, 0, 4'b1000);
      apb_write(12'h028, 3);
      issue_power(3, 2); wait_domain_state(3, 3); check_domain_error(3, 8);

      // RETENTION timeout
      reset_dut();
      apb_write(12'h028, 3);
      issue_power(3, 2); wait_domain_state(3, 2);
      vif.hold_retention[3] = 1'b1;
      issue_power(3, 1); wait_domain_state(3, 3); check_domain_error(3, 9);

      // QUIESCE timeout
      reset_dut();
      apb_write(12'h028, 3);
      issue_power(3, 2); wait_domain_state(3, 2);
      vif.mock_client_idle[3] = 1'b0;
      issue_power(3, 0); wait_domain_state(3, 3); check_domain_error(3, 4);
    endtask

    task automatic run_reset_during_activity();
      reset_dut(4'b1000);
      apb_write(12'h028, 0);
      issue_power(3, 2);
      repeat (3) @(posedge vif.pclk);
      vif.presetn = 1'b0;
      repeat (2) @(posedge vif.pclk);
      if ((vif.domain_state[7:6] != 0) || vif.power_req[3] ||
          vif.clock_enable[3] || vif.domain_reset_n[3] ||
          !vif.isolation[3] || vif.retention[3])
        `uvm_error("RESET_ACTIVE", "reset during power wait did not restore safe state")
      vif.presetn = 1'b1;
      repeat (3) @(posedge vif.pclk);
    endtask

    task automatic run_dvfs();
      bit [31:0] value;
      bit saw_voltage;
      bit saw_frequency;
      reset_dut();
      apb_write(12'h028, 8);
      bring_compute_on();

      // Same-OPP completes immediately without driving either external request.
      apb_write(12'h100, 32'h00000100);
      repeat (3) @(posedge vif.pclk);
      if (vif.voltage_req_valid || vif.frequency_req_valid || (vif.current_opp != 0))
        `uvm_error("DVFS_SAME", "same-OPP request used an external handshake")
      apb_read(12'h010, value);
      if (!value[5]) `uvm_error("DVFS_SAME", "same-OPP done event was not sticky")
      apb_write(12'h01c, 32'h000003ff);

      apb_write(12'h100, 32'h00000102);
      saw_voltage = 0;
      saw_frequency = 0;
      repeat (20) begin
        @(posedge vif.pclk);
        if (vif.voltage_req_valid) begin
          saw_voltage = 1;
          if (vif.voltage_code != 16'd900)
            `uvm_error("DVFS_UP", "wrong upscale voltage code")
        end
        if (vif.frequency_req_valid) begin
          if (!saw_voltage) `uvm_error("DVFS_UP", "frequency requested before voltage")
          saw_frequency = 1;
          if (vif.frequency_code != 16'd800)
            `uvm_error("DVFS_UP", "wrong upscale frequency code")
        end
        if (vif.current_opp == 2) break;
      end
      if (!saw_voltage || !saw_frequency || (vif.current_opp != 2))
        `uvm_error("DVFS_UP", "upscale sequence did not complete")

      apb_write(12'h100, 32'h00000100);
      saw_voltage = 0;
      saw_frequency = 0;
      repeat (20) begin
        @(posedge vif.pclk);
        if (vif.frequency_req_valid) saw_frequency = 1;
        if (vif.voltage_req_valid) begin
          if (!saw_frequency) `uvm_error("DVFS_DOWN", "voltage requested before frequency")
          saw_voltage = 1;
        end
        if (vif.current_opp == 0) break;
      end
      if (!saw_voltage || !saw_frequency || (vif.current_opp != 0))
        `uvm_error("DVFS_DOWN", "downscale sequence did not complete")

      // Thermal wins over simultaneous performance request.
      @(negedge vif.pclk);
      vif.thermal_req_opp   = 1;
      vif.thermal_req_valid = 1;
      vif.perf_req_opp      = 3;
      vif.perf_req_valid    = 1;
      @(negedge vif.pclk);
      vif.thermal_req_valid = 0;
      vif.perf_req_valid    = 0;
      wait_current_opp(1);

      // Illegal OPP from the three-bit external request.
      @(negedge vif.pclk);
      vif.thermal_req_opp   = 7;
      vif.thermal_req_valid = 1;
      @(negedge vif.pclk);
      vif.thermal_req_valid = 0;
      repeat (2) @(posedge vif.pclk);
      apb_read(12'h010, value);
      if (!value[8]) `uvm_error("ILLEGAL_OPP", "illegal OPP IRQ bit not set")

      // Unsafe firmware request while COMPUTE is OFF.
      reset_dut();
      apb_write(12'h100, 32'h00000101);
      repeat (3) @(posedge vif.pclk);
      apb_read(12'h010, value);
      if (!value[6]) `uvm_error("DVFS_UNSAFE", "unsafe DVFS error not reported")

      // Voltage timeout preserves current OPP.
      reset_dut(0, 0, 0, 0, 0, 1, 0);
      apb_write(12'h028, 3);
      bring_compute_on();
      apb_write(12'h100, 32'h00000101);
      repeat (12) @(posedge vif.pclk);
      if (vif.current_opp != 0) `uvm_error("VOLT_TIMEOUT", "voltage timeout changed OPP")
      apb_read(12'h010, value);
      if (!value[7]) `uvm_error("VOLT_TIMEOUT", "DVFS timeout IRQ not set")

      // PLL timeout preserves current OPP.
      reset_dut(0, 0, 0, 0, 0, 0, 1);
      apb_write(12'h028, 3);
      bring_compute_on();
      apb_write(12'h100, 32'h00000101);
      repeat (16) @(posedge vif.pclk);
      if (vif.current_opp != 0) `uvm_error("PLL_TIMEOUT", "PLL timeout changed OPP")
      apb_read(12'h024, value);
      if (value[3:0] != 13) `uvm_error("PLL_TIMEOUT", "last error is not PLL timeout")
    endtask

    task automatic run_dvfs_busy_side_effects();
      bit [31:0] value;
      bit [31:0] ignored;
      bit slverr;
      reset_dut(0, 0, 0, 0, 0, 1, 0);
      apb_write(12'h028, 0);
      bring_compute_on();
      apb_write(12'h100, 32'h00000101);
      wait (vif.voltage_req_valid);

      apb_transfer(1'b1, 12'h110, 32'h012c0302, ignored, slverr);
      if (!slverr) `uvm_error("OPP_BUSY", "OPP write while DVFS busy did not assert PSLVERR")
      apb_read(12'h110, value); expect_equal(value, 32'h01900320, "OPP0 preserved while busy");

      apb_write(12'h100, 32'h00000102);
      repeat (3) @(posedge vif.pclk);
      apb_read(12'h024, value);
      if (value[3:0] != 10) `uvm_error("DVFS_BUSY", "busy DVFS command did not record ERR_BUSY")
      apb_read(12'h010, value);
      if (!value[9]) `uvm_error("DVFS_BUSY", "busy DVFS command IRQ was not sticky")

      vif.hold_voltage = 1'b0;
      wait_current_opp(1);
    endtask

    task automatic run_override();
      bit [31:0] value;
      reset_dut();
      vif.test_override = 1'b1;
      repeat (2) @(posedge vif.pclk);
      if ((vif.power_req != 4'hf) || (vif.clock_enable != 4'hf) ||
          (vif.domain_reset_n != 4'hf) || (vif.isolation != 4'h0) ||
          (vif.retention != 4'h0))
        `uvm_error("OVERRIDE", "external test override outputs incorrect")
      issue_power(3, 2);
      repeat (4) @(posedge vif.pclk);
      apb_read(12'h010, value);
      if (!value[9]) `uvm_error("OVERRIDE", "command during override did not report busy")
      if (vif.domain_state[7:6] != 0)
        `uvm_error("OVERRIDE", "override changed functional state")
      vif.test_override = 1'b0;
      repeat (2) @(posedge vif.pclk);

      apb_write(12'h02c, 1);
      repeat (2) @(posedge vif.pclk);
      if ((vif.power_req != 4'hf) || (vif.clock_enable != 4'hf) ||
          (vif.domain_reset_n != 4'hf) || (vif.isolation != 4'h0) ||
          (vif.retention != 4'h0))
        `uvm_error("CSR_OVERRIDE", "CSR test override outputs incorrect")
      apb_write(12'h02c, 0);
      repeat (2) @(posedge vif.pclk);
    endtask
  endclass

  class rscu_reset_csr_test extends rscu_base_test;
    `uvm_component_utils(rscu_reset_csr_test)
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    task run_phase(uvm_phase phase);
      phase.raise_objection(this);
      run_reset_csr();
      `uvm_info("PASS", "DV_TESTCASE_PASS:RSCU_RESET_CSR", UVM_NONE)
      phase.drop_objection(this);
    endtask
  endclass

  class rscu_power_test extends rscu_base_test;
    `uvm_component_utils(rscu_power_test)
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    task run_phase(uvm_phase phase);
      phase.raise_objection(this);
      run_power_normal();
      run_dependency_and_deny();
      `uvm_info("PASS", "DV_TESTCASE_PASS:RSCU_POWER", UVM_NONE)
      phase.drop_objection(this);
    endtask
  endclass

  class rscu_error_test extends rscu_base_test;
    `uvm_component_utils(rscu_error_test)
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    task run_phase(uvm_phase phase);
      phase.raise_objection(this);
      run_power_timeouts();
      `uvm_info("PASS", "DV_TESTCASE_PASS:RSCU_ERRORS", UVM_NONE)
      phase.drop_objection(this);
    endtask
  endclass

  class rscu_dvfs_test extends rscu_base_test;
    `uvm_component_utils(rscu_dvfs_test)
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    task run_phase(uvm_phase phase);
      phase.raise_objection(this);
      run_dvfs();
      `uvm_info("PASS", "DV_TESTCASE_PASS:RSCU_DVFS", UVM_NONE)
      phase.drop_objection(this);
    endtask
  endclass

  class rscu_q3_test extends rscu_base_test;
    `uvm_component_utils(rscu_q3_test)
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    task run_phase(uvm_phase phase);
      phase.raise_objection(this);
      run_reset_csr();
      run_csr_irq_side_effects();
      run_power_normal();
      run_dependency_and_deny();
      run_power_edge_cases();
      run_power_timeouts();
      run_reset_during_activity();
      run_dvfs();
      run_dvfs_busy_side_effects();
      run_override();
      `uvm_info("PASS", "DV_TESTCASE_PASS:RSCU_Q3", UVM_NONE)
      phase.drop_objection(this);
    endtask
  endclass
endpackage
