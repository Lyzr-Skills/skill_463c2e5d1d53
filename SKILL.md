---
name: anaplan-gap-detection
description: Detect missing data after Anaplan MCP sql_query calls and plan the follow-up queries, looping until every requested cell and line item has been retrieved. Use this skill whenever an Anaplan data retrieval agent has a table payload (table_id, module, sql_table, select, dimensions) and sql_query results, and needs to know which time periods, dimension members (e.g. parent/non-leaf members missed by _is_leaf filters) or select columns did not come back. Replaces manual discovery queries and hand-built gap query lists. Trigger on any Anaplan retrieval run, CSH/forecast module pulls, "gap queries", "missing members", "coverage check", or when sql_query output looks incomplete or truncated at LIMIT 500.
---

# Anaplan gap detection

`scripts/gap_detect.py` compares the requested payload with the verbatim `sql_query`
outputs and returns:

- `status`: `incomplete` (more to fetch), `complete` (everything received), or
  `exhausted` (what remains was explicitly re-requested and Anaplan returned nothing)
- `next_payloads`: follow-up payloads with the **same structure as the input**, each
  covering only missing data, plus a `gap_batch` id
- `suggested_queries`: ready SQL for those payloads, already compliant with the Global
  SQL rules (LIMIT 500, no IN/OR/DISTINCT/GROUP BY, every dimension sliced, quoted identifiers)
- `missing`: line items never returned, and dimension members never returned
- `no_data`, `errors`, `warnings` (failed calls, rows that hit LIMIT 500, unattributable rows)

It is deterministic, so the model never has to diff 500-row JSON by eye. This is why it
replaces the discovery and gap-list steps: those steps were where members got skipped
or the counts drifted.

## How the detection works

1. Every requested cell is the cartesian product of the payload's dimension values
   (e.g. `Current Forecast` × 13 time values = 13 cells).
2. Each returned row is attributed to a cell using its dimension columns. Matching is
   trimmed and case-insensitive, and time labels are normalised, so `2027-02-01T00:00:00`,
   `Feb 27`, `Feb-27` and `February 2027` are the same period; `FY2025` = `FY25`.
   If a dimension column is not in the row, the script falls back to an equality filter in
   the query text, then to the dimension's single value.
3. A cell is complete when every `select` line item is present (a returned `0.0` or `null`
   counts as received; `--null-is-missing` changes that). Rows for unrequested members
   (e.g. FY26 from a year-level query) are ignored.
4. Missing cells are grouped by their exact set of missing line items, then partitioned
   into rectangles. Each rectangle becomes one payload, so nothing already received is
   re-requested.
5. Retries escalate: the first pass may slice time by `time_levels` and multi-value
   dimensions by `_is_leaf`; any cell that is requested again gets exact equality filters on
   every dimension. After `--max-attempts` (default 2) explicit re-requests with no data, a
   cell moves to `no_data` and the loop can end.

## Workflow

Keep one working folder per table, e.g. `/tmp/anaplan/<table_id>/`, holding
`payload.json`, `state.json` and an `outputs/` folder.

**Round 0: plan the initial queries.** Save the input payload, then run without outputs.
Pass `--leaf-dims` for multi-value dimensions whose `<dim>_is_leaf` column appears in the
`sql_schema` result, so the first pass uses one leaf query instead of one per member.

```bash
python scripts/gap_detect.py --payload /tmp/anaplan/TBL_001/payload.json \
  --state /tmp/anaplan/TBL_001/state.json --leaf-dims entity --compact
```

**Each round:**

1. Execute every entry of `suggested_queries` with `sql_query` (batches of up to 8 parallel
   calls), using the same `included_objects` as for `sql_schema`.
2. Save each tool output **verbatim**, unedited, one file per call:
   `outputs/r<round>_q<n>.json`. Error outputs are saved too; the script reports them.
3. Re-run the script with the same payload and state, pointing `--outputs` at the folder
   (cumulative outputs are fine; found data is also kept in the state file):
   ```bash
   python scripts/gap_detect.py --payload /tmp/anaplan/TBL_001/payload.json \
     --outputs /tmp/anaplan/TBL_001/outputs --state /tmp/anaplan/TBL_001/state.json --compact
   ```
4. If `status` is `incomplete`, start the next round with the new `suggested_queries`.
   Stop on `complete` or `exhausted`, or after 6 rounds as a safety cap.

Always pass the **original** payload, never a `next_payloads` entry: coverage is measured
against the original request. `next_payloads` is there for logging, or for orchestrators
that dispatch each gap batch as a separate agent task.

**Error handling.** If `errors` lists a "column not found" or validation error, check the
query against the `sql_schema` result, correct it and run it once more in the next batch.
Errors do not need special bookkeeping: affected cells stay missing and are re-planned.
If the script itself prints `{"status": "error", ...}`, the payload or state file is
malformed; fix the input rather than editing the state by hand.

**Truncation.** A warning that a query returned 500 rows means data may have been cut off.
The resulting missing cells are re-requested with narrower, exact filters automatically.

## Using it from code

If an orchestrator already collects tool outputs (rather than the agent), import it:

```python
from gap_detect import detect_gaps
result, state = detect_gaps(payload, outputs, state, leaf_dims=["entity"])
while result["status"] == "incomplete":
    outputs = [call_sql_query(q["sql"]) for q in result["suggested_queries"]]
    result, state = detect_gaps(payload, outputs, state)
```

`outputs` items can be in any form the MCP returns: the content-block list
(`[{"type": "text", "text": "{\"query\": ..., \"results\": [...]}"}]`), the inner JSON
string, a parsed `{"query", "results"}` dict, an `isError` result, or a list of these.

## Files

- `scripts/gap_detect.py`: the detector (standard library only, Python 3.8+)
- `scripts/test_gap_detect.py`: self-tests; run `python scripts/test_gap_detect.py`
- `references/agent_system_prompt.md`: the retrieval agent's system prompt with Steps 6–9
  rewritten around this skill. Read it when updating the agent's prompt.
- `examples/`: the TBL_001 payload and a real year-level `sql_query` output
