module pck_dependency_checker #(
  parameter int unsigned NUM_DOMAINS = 4
) (
  input  logic [2:0]                     domain_id_i,
  input  logic [1:0]                     target_state_i,
  input  logic [NUM_DOMAINS*2-1:0]       domain_state_i,
  input  logic [NUM_DOMAINS*NUM_DOMAINS-1:0] parent_masks_i,
  output logic                           legal_o
);
  import rscu_pkg::*;

  integer candidate;
  logic [1:0] candidate_state;
  logic parent_selected;
  logic child_depends_on_selected;

  always_comb begin
    legal_o = 1'b1;
    if (domain_id_i >= NUM_DOMAINS) begin
      legal_o = 1'b0;
    end else if (target_state_i == PWR_ON) begin
      for (candidate = 0; candidate < NUM_DOMAINS; candidate = candidate + 1) begin
        parent_selected = parent_masks_i[(domain_id_i*NUM_DOMAINS)+candidate];
        candidate_state = domain_state_i[candidate*2 +: 2];
        if (parent_selected && (candidate_state != PWR_ON)) begin
          legal_o = 1'b0;
        end
      end
    end else if ((target_state_i == PWR_OFF) ||
                 (target_state_i == PWR_RETENTION)) begin
      for (candidate = 0; candidate < NUM_DOMAINS; candidate = candidate + 1) begin
        child_depends_on_selected =
          parent_masks_i[(candidate*NUM_DOMAINS)+domain_id_i];
        candidate_state = domain_state_i[candidate*2 +: 2];
        if (child_depends_on_selected && (candidate_state == PWR_ON)) begin
          legal_o = 1'b0;
        end
      end
    end else begin
      legal_o = 1'b0;
    end
  end
endmodule
