## SECTION 1 — TRACE HIERARCHY

**Is there a single unified root span per API request?**
No. There is no explicit root span created in the API endpoint handlers. The FastAPI instrumentation creates its own root span (via `FastAPIInstrumentor.instrument_app()`), but the business logic spans in `curator_service.py` are created independently without being explicitly nested under the FastAPI request span.

**Are child spans correctly nested under the root?**
The hierarchy is partially correct but has a critical flaw:
- `curator.fetch_reflector_row` (in `curator_input_store.py:53`) is created in a separate call context from `curator.prepare_dpo_dataset`
- When `store_output=True`, `curator.store_response` is created inside `curator.prepare_dpo_dataset` (line 494)
- However, `curator.fetch_reflector_row` is called from `main.py:55` BEFORE `curate_feedback` is invoked, making it a sibling to the main business logic span, not a child

**Hierarchy break identification:**
- **File:** `app/main.py`
- **Issue:** The `bigquery_input_store.fetch_by_request_id()` call (line 55) happens outside the `curate_feedback` span context. This creates a disconnected trace where the BigQuery fetch appears as a separate root span rather than a child of the main request processing.
- **Why:** The endpoint handler doesn't create a root span that encompasses both the fetch and processing operations.

---

## SECTION 2 — COVERAGE AUDIT

| Step | Span Name | File | Status |
|------|-----------|------|--------|
| 1. Receive request_id | (FastAPI auto-instrumented) | `main.py` | partial |
| 2. Fetch row from BigQuery | `curator.fetch_reflector_row` | `curator_input_store.py:53` | covered |
| 3. Extract prompt, generated, human impressions | `curator.extract_prompt` + `curator.normalize_feedback` | `curator_service.py:336, 345` | covered |
| 4. Normalize impressions with fallback | `curator.normalize_feedback` | `curator_service.py:345` | covered |
| 5. Assess DPO readiness | `curator.assess_readiness` | `curator_service.py:374` | covered |
| 6. Calculate quality scores | `curator.score_record` | `curator_service.py:411` | covered |
| 7. Build DPO training example | `curator.build_training_example` | `curator_service.py:393` | covered |
| 8. Store result in BigQuery | `curator.store_response` | `curator_output_store.py:60` | covered |
| 9. Return API response | (No dedicated span) | `curator_service.py` | missing |

**Missing coverage:**
- No span for the "skipped" early return path (lines 292-314) - the span is set but there's no explicit child span for the skip logic
- No span for the `build_training_example` function call itself (it's wrapped but the actual function is a pure function with no internal tracing)

---

## SECTION 3 — SPAN NAMING & CONSISTENCY

**Naming convention analysis:**
All spans follow a consistent `curator.<action>` pattern which is good for filtering in Phoenix.

**Meaningful span names:**
- ✅ `curator.fetch_reflector_row` - Clear, indicates BigQuery fetch
- ✅ `curator.prepare_dpo_dataset` - Good root span name
- ✅ `curator.extract_prompt` - Clear purpose
- ✅ `curator.normalize_feedback` - Clear purpose
- ✅ `curator.assess_readiness` - Clear purpose
- ✅ `curator.build_training_example` - Clear purpose
- ✅ `curator.score_record` - Clear purpose
- ✅ `curator.store_response` - Clear purpose

**Issues:**
- The span `curator.prepare_dpo_dataset` is used as the main processing span, but it's not explicitly created in the endpoint - it's created inside `curate_feedback`. This creates a gap in the trace hierarchy.
- No span for the "skip" path when `send_to_curator=False` - the logic just sets attributes on the parent span.

---

## SECTION 4 — ATTRIBUTE QUALITY

### `curator.fetch_reflector_row` (curator_input_store.py)
- **Captured:** request_id, bigquery_input_available, row_count, tenant_id, site_id, decision, parse_failed flags
- **Missing:** No timing/latency attribute, no error details beyond status
- **Excessive:** None
- **Score:** MEDIUM - Good coverage but could use latency

### `curator.prepare_dpo_dataset` (curator_service.py)
- **Captured:** request_id, tenant_id, site_id, decision, actual_route, expected_route, scope_keys, send_to_curator, has_inputs, has_findings_text, has_structured_findings, has_prompt, issue_count, reward_label, reward_score, all 3 scores, latency_ms, generator_output_source, fallback_used
- **Missing:** No error attributes for the skip path, no explicit error messages
- **Excessive:** None (no raw text captured)
- **Score:** HIGH - Excellent attribute coverage

### `curator.extract_prompt` (curator_service.py)
- **Captured:** prompt_present, prompt_length
- **Missing:** The actual prompt text is NOT captured (good for privacy), but could add prompt_source for debugging
- **Excessive:** None
- **Score:** MEDIUM - Basic but sufficient

### `curator.normalize_feedback` (curator_service.py)
- **Captured:** generated_impression_present, generator_output_source, fallback_used, final_impression_present, human_impression_present
- **Missing:** No indication of which fallback path was taken (e.g., "used_fallback_path_2_of_4")
- **Excessive:** None
- **Score:** MEDIUM - Good but could be more specific about fallback chain

### `curator.assess_readiness` (curator_service.py)
- **Captured:** has_generated_output, has_human_output, has_prompt, has_dpo_pair, is_dpo_ready, missing_fields_count
- **Missing:** The actual missing_fields list is not captured (only count)
- **Excessive:** None
- **Score:** MEDIUM - Missing the actual missing field names

### `curator.build_training_example` (curator_service.py)
- **Captured:** has_training_example
- **Missing:** No details about what was built, no metadata about the training example
- **Excessive:** None
- **Score:** LOW - Very minimal attributes

### `curator.score_record` (curator_service.py)
- **Captured:** data_completeness_score, prompt_completeness_score, dpo_readiness_score
- **Missing:** No breakdown of how scores were calculated
- **Excessive:** None
- **Score:** MEDIUM - Basic score capture

### `curator.store_response` (curator_output_store.py)
- **Captured:** request_id, is_dpo_ready, has_dpo_pair, stored
- **Missing:** No error details, no row count, no latency
- **Excessive:** None
- **Score:** MEDIUM - Adequate but could be more detailed

---

## SECTION 5 — ERROR HANDLING

**record_exception() usage:**
- ✅ `curator_input_store.py:129` - Uses `record_exception(e)` in the catch block
- ✅ `curator_output_store.py:78` - Uses `record_exception(e)` in the catch block
- ✅ `curator_service.py:507` - Uses `record_exception(e)` in the catch block

**StatusCode.ERROR usage:**
- ✅ `curator_input_store.py:60, 130` - Sets ERROR status appropriately
- ✅ `curator_output_store.py:70, 79` - Sets ERROR status appropriately
- ✅ `curator_service.py:498, 508` - Sets ERROR status appropriately

**Silent failure points:**
- ⚠️ `curator_input_store.py:108-113` - JSON parse failures are logged and a flag is set, but `record_exception()` is NOT called. This could hide data quality issues.
- ⚠️ `curator_input_store.py:39-40` - Initialization failures are only logged, no span is created to track this.
- ⚠️ `curator_output_store.py:45-47` - Initialization failures are only logged, no span created.
- ⚠️ `curator_service.py:292-314` - The skip path doesn't set StatusCode.ERROR (correctly uses OK), but there's no explicit error handling for edge cases in the skip logic.

---

## SECTION 6 — EFFICIENCY & COST

**Raw text in attributes:**
- ✅ No raw impression text, prompt text, or large JSON payloads are captured in span attributes. This is correct for cost efficiency.

**Redundant attributes:**
- ⚠️ `curator.prepare_dpo_dataset` sets attributes that are also set on child spans (e.g., `has_generated_output`, `has_human_output`, `is_dpo_ready` are duplicated)
- ⚠️ `generator_output_source` and `fallback_used` are set on both `normalize_feedback` and `prepare_dpo_dataset` spans

**Cost efficiency assessment:**
- The strategy is reasonably cost-efficient. No large payloads are captured.
- However, the redundant attributes across parent/child spans add unnecessary overhead.
- The trace would be acceptable for production Phoenix/Arize deployment.

---

## SECTION 7 — PHOENIX UI SIMULATION

```
Trace Tree for ONE end-to-end API call:

root: POST /v1/ace/curator:review-human-feedback (FastAPI auto-instrumented)
├── curator.fetch_reflector_row
│   Attributes: request_id, row_count, tenant_id, decision, status=OK
│   Latency: ~50-200ms (BigQuery query)
│
└── curator.prepare_dpo_dataset
    Attributes: request_id, tenant_id, site_id, decision, send_to_curator=True, 
                is_dpo_ready=True/False, latency_ms, all 3 scores
    Latency: ~5-20ms (processing)
    │
    ├── curator.extract_prompt
    │   Attributes: prompt_present=True/False, prompt_length=1234
    │   Latency: <1ms
    │
    ├── curator.normalize_feedback
    │   Attributes: generated_impression_present=True, human_impression_present=True,
    │               generator_output_source="generated_impression", fallback_used=False
    │   Latency: <1ms
    │
    ├── curator.assess_readiness
    │   Attributes: has_prompt=True, has_generated_output=True, has_human_output=True,
    │               has_dpo_pair=True, is_dpo_ready=True, missing_fields_count=0
    │   Latency: <1ms
    │
    ├── curator.build_training_example
    │   Attributes: has_training_example=True/False
    │   Latency: <1ms
    │
    ├── curator.score_record
    │   Attributes: data_completeness_score=1.0, prompt_completeness_score=1.0,
    │               dpo_readiness_score=1.0
    │   Latency: <1ms
    │
    └── curator.store_response (only if store_output=True)
        Attributes: request_id, is_dpo_ready=True, has_dpo_pair=True, stored=True
        Latency: ~20-100ms (BigQuery insert)
```

**Disconnected traces:**
- `curator.fetch_reflector_row` appears as a separate root trace because it's called outside the `curate_feedback` span context
- The FastAPI root span and `curator.prepare_dpo_dataset` are siblings, not parent-child

---

## SECTION 8 — PRODUCTION READINESS VERDICT

**[ ] Not production ready**

### What must be fixed before going to production (priority ordered):

1. **CRITICAL: Fix trace hierarchy** - The `curator.fetch_reflector_row` span must be nested under the main request processing. Either:
   - Create a root span in the endpoint handler that encompasses both fetch and processing, OR
   - Move the fetch call inside `curate_feedback` to maintain proper nesting

2. **CRITICAL: Add record_exception() for JSON parse failures** - `curator_input_store.py:108-113` silently fails on JSON parsing without calling `record_exception()`, making data quality issues invisible in Phoenix.

3. **HIGH: Capture missing_fields list** - `curator.assess_readiness` only captures the count, not the actual field names. An on-call engineer needs to know WHICH fields are missing.

4. **HIGH: Add latency to store_response span** - No timing information for BigQuery write operations.

5. **MEDIUM: Remove redundant attributes** - `has_generated_output`, `has_human_output`, `is_dpo_ready` are duplicated across parent and child spans.

6. **MEDIUM: Add prompt_source attribute** - When prompt is extracted, capture which fallback path was used for debugging.

7. **LOW: Add explicit span for skip path** - When `send_to_curator=False`, create a dedicated span for the skip logic.

### What is already done well:

- ✅ Consistent span naming convention (`curator.<action>`)
- ✅ No raw text or large payloads in attributes (cost-efficient)
- ✅ Proper use of `record_exception()` and `StatusCode.ERROR` in most places
- ✅ Good attribute coverage on the main `prepare_dpo_dataset` span
- ✅ Clear separation of concerns with dedicated spans for each pipeline step
- ✅ Proper error status codes for failure paths
- ✅ Latency measurement on the main processing span