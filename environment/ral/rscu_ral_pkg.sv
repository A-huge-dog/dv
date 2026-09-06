package rscu_ral_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  class rscu_reg32 extends uvm_reg;
    `uvm_object_utils(rscu_reg32)
    uvm_reg_field data;
    string access_kind = "RW";
    uvm_reg_data_t reset_value = 0;

    function new(string name = "rscu_reg32");
      super.new(name, 32, UVM_NO_COVERAGE);
    endfunction

    virtual function void build();
      data = uvm_reg_field::type_id::create("data");
      data.configure(this, 32, 0, access_kind, 0, reset_value, 1, 0, 0);
    endfunction
  endclass

  class rscu_reg_block extends uvm_reg_block;
    `uvm_object_utils(rscu_reg_block)
    rscu_reg32 id_reg;
    rscu_reg32 version_reg;
    rscu_reg32 capability_reg;
    rscu_reg32 global_ctrl_reg;
    rscu_reg32 irq_raw_reg;
    rscu_reg32 irq_mask_reg;
    rscu_reg32 irq_status_reg;
    rscu_reg32 irq_clear_reg;
    rscu_reg32 error_status_reg;
    rscu_reg32 error_info_reg;
    rscu_reg32 timeout_cfg_reg;
    rscu_reg32 debug_override_reg;
    rscu_reg32 gpr_ctrl_reg[4];
    rscu_reg32 gpr_status_reg[4];
    rscu_reg32 domain_cmd_reg;
    rscu_reg32 domain_status_reg;
    rscu_reg32 domain_states_reg;
    rscu_reg32 domain_busy_reg;
    rscu_reg32 dep_mask_reg[4];
    rscu_reg32 dvfs_cmd_reg;
    rscu_reg32 dvfs_status_reg;
    rscu_reg32 opp_reg[4];

    function new(string name = "rscu_reg_block");
      super.new(name, UVM_NO_COVERAGE);
    endfunction

    local function rscu_reg32 make_reg(
      string name,
      uvm_reg_addr_t offset,
      string access_kind,
      uvm_reg_data_t reset_value
    );
      rscu_reg32 reg_handle;
      string map_access;
      reg_handle = rscu_reg32::type_id::create(name);
      reg_handle.access_kind = access_kind;
      reg_handle.reset_value = reset_value;
      reg_handle.configure(this, null, "");
      reg_handle.build();
      map_access = access_kind;
      if ((access_kind != "RO") && (access_kind != "WO")) begin
        map_access = "RW";
      end
      default_map.add_reg(reg_handle, offset, map_access);
      return reg_handle;
    endfunction

    virtual function void build();
      default_map = create_map("apb_map", 0, 4, UVM_LITTLE_ENDIAN, 1);
      id_reg            = make_reg("id",             'h000, "RO",  'h52534355);
      version_reg       = make_reg("version",        'h004, "RO",  'h00010000);
      capability_reg    = make_reg("capability",     'h008, "RO",  'h04040401);
      global_ctrl_reg   = make_reg("global_ctrl",    'h00c, "RW",  'h00000001);
      irq_raw_reg       = make_reg("irq_raw",        'h010, "RO",  0);
      irq_mask_reg      = make_reg("irq_mask",       'h014, "RW",  0);
      irq_status_reg    = make_reg("irq_status",     'h018, "RO",  0);
      irq_clear_reg     = make_reg("irq_clear",      'h01c, "WO",  0);
      error_status_reg  = make_reg("error_status",   'h020, "W1C", 0);
      error_info_reg    = make_reg("error_info",     'h024, "RO",  0);
      timeout_cfg_reg   = make_reg("timeout_cfg",    'h028, "RW",  32);
      debug_override_reg = make_reg("debug_override", 'h02c, "RW", 0);
      gpr_ctrl_reg[0]   = make_reg("gpr_ctrl0",      'h040, "RW",  0);
      gpr_ctrl_reg[1]   = make_reg("gpr_ctrl1",      'h044, "RW",  0);
      gpr_ctrl_reg[2]   = make_reg("gpr_ctrl2",      'h048, "RW",  0);
      gpr_ctrl_reg[3]   = make_reg("gpr_ctrl3",      'h04c, "RW",  0);
      gpr_status_reg[0] = make_reg("gpr_status0",    'h060, "RO",  0);
      gpr_status_reg[1] = make_reg("gpr_status1",    'h064, "RO",  0);
      gpr_status_reg[2] = make_reg("gpr_status2",    'h068, "RO",  0);
      gpr_status_reg[3] = make_reg("gpr_status3",    'h06c, "RO",  0);
      domain_cmd_reg    = make_reg("domain_cmd",     'h080, "WO",  0);
      domain_status_reg = make_reg("domain_status",  'h084, "RO",  0);
      domain_states_reg = make_reg("domain_states",  'h088, "RO",  0);
      domain_busy_reg   = make_reg("domain_busy",    'h08c, "RO",  0);
      dep_mask_reg[0]   = make_reg("dep_mask0",      'h0a0, "RO",  'h00000006);
      dep_mask_reg[1]   = make_reg("dep_mask1",      'h0a4, "RO",  'h00000008);
      dep_mask_reg[2]   = make_reg("dep_mask2",      'h0a8, "RO",  'h00000008);
      dep_mask_reg[3]   = make_reg("dep_mask3",      'h0ac, "RO",  0);
      dvfs_cmd_reg      = make_reg("dvfs_cmd",       'h100, "WO",  0);
      dvfs_status_reg   = make_reg("dvfs_status",    'h104, "RO",  0);
      opp_reg[0]        = make_reg("opp0",            'h110, "RW",  'h01900320);
      opp_reg[1]        = make_reg("opp1",            'h114, "RW",  'h02580352);
      opp_reg[2]        = make_reg("opp2",            'h118, "RW",  'h03200384);
      opp_reg[3]        = make_reg("opp3",            'h11c, "RW",  'h03e803e8);
      lock_model();
    endfunction
  endclass
endpackage
