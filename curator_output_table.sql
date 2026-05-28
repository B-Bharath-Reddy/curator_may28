CREATE TABLE IF NOT EXISTS `project_id.ace_curator.dpo_candidates`
(
  request_id          STRING    NOT NULL,
  created_at          TIMESTAMP NOT NULL,
  event_id            STRING,

  tenant_id           STRING,
  site_id             STRING,
  decision            STRING,

  has_dpo_pair        BOOL,
  is_dpo_ready        BOOL,
  missing_fields      JSON,

  reward_score        FLOAT64,
  reward_label        STRING,

  latency_ms          FLOAT64,

  -- Clean DPO payload. Populated only when is_dpo_ready = true.
  -- Shape: {"prompt": "...", "chosen": "...", "rejected": "..."}
  dpo_record          JSON,

  -- Full Curator response for audit/debug/reporting.
  curator_response    JSON
)
PARTITION BY DATE(created_at)
CLUSTER BY tenant_id, is_dpo_ready, has_dpo_pair, reward_label
OPTIONS (
  description = "Lean Curator output table for DPO candidate preparation. Stores filterable readiness fields, clean DPO record, and full Curator response JSON."
);
