# Curator Agent Workflow



---

## 1. Overview

The Curator service processes feedback data from the Reflector system.

**Flow:**
```
request_id → Fetch from BigQuery → Process → Evaluate Readiness → Store → Return Response
```

**Goal:**
- Prepare data for DPO training
- Evaluate if data is DPO-ready
- Store processed output for downstream use (DPO)

---

## 2. What Was Implemented

- Fetch Reflector data using `request_id` from BigQuery
- Parse and validate input data
- Extract prompt, generated output, and human output
- Apply fallback logic for missing fields
- Evaluate DPO readiness
- Calculate scores: `data_completeness_score`, `prompt_completeness_score`, `dpo_readiness_score`
- Store processed result in BigQuery (for request-id flow)
- Return structured API response
- OpenTelemetry tracing for all key steps
- Upstream filtering using `send_to_curator` flag — only rows marked as relevant by Reflector are processed; non-relevant rows are skipped from processing and storage, but a response is still returned for visibility

---

## 3. What Was Tested

| Area | Status |
| --- | --- |
| Data fetching from BigQuery using `request_id` |  Passed |
| Handling of valid and invalid input data |  Passed |
| Fallback logic for missing generated output |  Passed |
| DPO readiness evaluation |  Passed |
| Score calculation logic |  Passed |
| API response structure |  Passed |
| Output storage in BigQuery |  Passed |

**Testing:**

Test cases have been implemented against core Curator logic, including:
- End-to-end data processing flow
- DPO readiness evaluation
- Fallback handling and edge cases
- API behavior and storage integration

All core functionalities are verified and working as expected.

---

## 4. Sample API Response

### Direct Endpoint (Tested with Full Payload)
`POST /v1/ace/curator:review-human-feedback-direct`

### Response (from Swagger)

```json
{
  "request_id": "reflector-gen-vs-human-001",
  "tenant_id": "test-DH",
  "decision": "CRITICAL_FAIL",
  "impressions": {
    "generated_impression": "1. Severe bilateral pneumothorax...",
    "final_impression": "1. Severe right pneumothorax...",
    "human_impression": "1. Severe bilateral pneumothorax..."
  },
  "reward_score": 0.15,
  "reward_label": "acceptable",
  "issue_codes": ["BAD_ROUTE", "MISSING_BILATERAL_PNEUMOTHORAX"],
  "prompt_text": null,
  "scores": {
    "data_completeness_score": 0.8,
    "prompt_completeness_score": 0,
    "dpo_readiness_score": 0
  },
  "training_example": null,
  "readiness": {
    "is_dpo_ready": false,
    "missing_fields": ["prompt_text"]
  },
  "trace": {
    "status": "incomplete",
    "latency_ms": 0.1
  }
}
```



## 5. Sample API Response (Production Flow)

### Endpoint
```POST /v1/ace/curator:review-human-feedback```

### Request (curl)

```bash
curl -X POST "http://127.0.0.1:8000/v1/ace/curator:review-human-feedback" \
 -H "accept: application/json" \
 -H "Content-Type: application/json" \
 -d '{
  "request_id": "reflector-gen-vs-human-001"
}'
```

### Response (from Swagger)

```json
{
  "request_id": "reflector-gen-vs-human-001",
  "tenant_id": "test-DH",
  "decision": "CRITICAL_FAIL",
  "scores": {
    "data_completeness_score": 0.8,
    "prompt_completeness_score": 0,
    "dpo_readiness_score": 0
  },
  "training_example": null,
  "readiness": {
    "is_dpo_ready": false,
    "missing_fields": ["prompt_text"]
  },
  "trace": {
    "status": "incomplete",
    "latency_ms": 0.04
  }
}
```
---


---

## 6. Scores

### When prompt is missing (current state):
- Prompt is missing → reduces completeness score  
- DPO readiness fails due to missing prompt 

```
data_completeness_score:   0.8
prompt_completeness_score: 0.0
dpo_readiness_score:       0.0
```

### When prompt is available (expected after upstream fix):
- All required fields present  
- Record becomes fully DPO-ready  
```
data_completeness_score:   1.0
prompt_completeness_score: 1.0
dpo_readiness_score:       1.0
```

> Scores correctly reflect missing prompt or incomplete data.

---

## 7. Traceability (Phoenix)

OpenTelemetry spans implemented for:

| Span | Purpose |
| --- | --- |
| `curator.fetch_reflector_row` | BigQuery fetch |
| `curator.extract_prompt` | Prompt extraction |
| `curator.normalize_feedback` | Impression normalization |
| `curator.assess_readiness` | Readiness evaluation |
| `curator.score_record` | Score calculation |
| `curator.build_training_example` | DPO example creation |
| `curator.store_response` | BigQuery store |

> Phoenix tracing is implemented in code but not validated end-to-end — Phoenix credentials/collector access were not available during testing.



---

## 8. Observations

- System handles missing data safely
- Fallback logic works correctly
- Non-DPO-ready records are still stored for audit
- Scores accurately reflect data completeness
- End-to-end DB flow verified with real BigQuery credentials

---

## 9. Current Limitations

| Limitation | Impact |
| --- | --- |
| Prompt not available in upstream BigQuery | Blocking full DPO readiness |
| Phoenix tracing not verified | No live trace visibility yet |
| preferred_output policy pending confirmation | `human_impression` assumed as chosen output |

---

## 10. Conclusion

| Item | Status |
| --- | --- |
| Core Curator functionality |  Working |
| Data fetch, processing, scoring, storage |  Validated |
| Full DPO readiness | Pending prompt from upstream |
| Phoenix tracing | Pending credentials/access |



### Note

For testing, I used my own GCP BigQuery environment, as access to the shared BigQuery project requires proper authentication.

The Reflector input table schema was recreated based on the defined production schema to ensure compatibility with the actual structure. A sample record was inserted to validate the data fetching logic.

Additionally, a separate BigQuery table was created to store Curator responses, and the end-to-end pipeline (fetch → process → store) was verified using sample/mock data and controlled payloads.

This approach allowed independent validation of the Curator workflow while a few clarifications (particularly around prompt handling) are still pending. I’ll be happy to share the setup and walkthrough once we connect.

