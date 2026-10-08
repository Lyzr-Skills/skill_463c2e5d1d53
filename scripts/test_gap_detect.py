#!/usr/bin/env python3
"""Self-tests: python test_gap_detect.py  (exits non-zero on failure)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gap_detect import detect_gaps, parse_time  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "..", "examples")
MONTHS = ["Feb 27", "Mar 27", "Apr 27", "May 27", "Jun 27", "Jul 27", "Aug 27",
          "Sep 27", "Oct 27", "Nov 27", "Dec 27", "Jan 28"]


def load(name):
    with open(os.path.join(EX, name)) as fh:
        return json.load(fh)


def wrap(rows, query="SELECT ... LIMIT 500"):
    """Mimic the MCP tool output exactly: content blocks with JSON text."""
    return [{"type": "text", "text": json.dumps({"query": query, "results": rows})}]


def test_time_parsing():
    assert parse_time("2027-02-01T00:00:00")[1] == parse_time("Feb 27")[1] == parse_time("February 2027")[1]
    assert parse_time("Q2 FY25") == ("quarter", "Q:25-2", "Q2 FY25")
    assert parse_time("FY2025")[1] == parse_time("FY25")[1]
    assert parse_time("Feb-27")[2] == "Feb 27"
    assert parse_time("Total")[0] == "unknown"


def test_user_example_round_1():
    payload = load("payload_TBL_001.json")
    res, st = detect_gaps(payload, [load("sql_output_year_level.json")])
    assert res["status"] == "incomplete"
    assert res["summary"]["complete_cells"] == 1            # FY25
    assert res["summary"]["rows_outside_request"] == 3      # FY26-FY28 ignored
    assert len(res["next_payloads"]) == 1
    nxt = res["next_payloads"][0]
    assert nxt["dimensions"][1]["values"] == MONTHS
    assert nxt["select"] == payload["select"]               # every column still needed
    assert len(res["suggested_queries"]) == 1
    assert "\"time_levels\" = 'month'" in res["suggested_queries"][0]["sql"]
    return payload, st


def test_partial_columns_and_missing_months():
    payload, st = test_user_example_round_1()
    cols = payload["select"][:-2]
    rows = []
    for m in MONTHS[:10]:                                   # Dec 27, Jan 28 missing
        r = {c: 1.0 for c in cols}
        if m in ("Feb 27", "Mar 27"):
            del r["vat"]                                    # column missing for 2 months
        r.update(time=m, versions="Current Forecast")
        rows.append(r)
    res, st = detect_gaps(payload, [wrap(rows)], st)
    assert res["status"] == "incomplete"
    batches = {tuple(p["dimensions"][1]["values"]): p for p in res["next_payloads"]}
    assert ("Feb 27", "Mar 27") in batches and ("Dec 27", "Jan 28") in batches
    vat_only = batches[("Feb 27", "Mar 27")]
    assert vat_only["select"] == ["vat", "time", "versions"]
    # retries use exact time equality, never IN/OR
    sqls = [q["sql"] for q in res["suggested_queries"]]
    assert all(" IN " not in s and " OR " not in s and s.endswith("LIMIT 500") for s in sqls)
    assert any("\"time\" = 'Dec 27'" in s for s in sqls)
    return payload, st


def test_exhaustion_after_max_attempts():
    payload, st = test_partial_columns_and_missing_months()
    cols = payload["select"][:-2]
    fix = [dict(vat=0.0, time=m, versions="Current Forecast") for m in ("Feb 27", "Mar 27")]
    # Dec/Jan were re-requested twice (month-level gap query, then exact retry) -> no data
    res, st = detect_gaps(payload, [wrap(fix)], st)
    assert res["status"] == "exhausted", res["status"]
    assert res["next_payloads"] == []
    assert {d["dimensions"]["time"] for d in res["no_data"]} == {"Dec 27", "Jan 28"}
    res2, _ = detect_gaps(payload, [wrap(fix)], st, max_attempts=5)   # state is sticky
    assert res2["status"] == "exhausted"
    _ = cols


def test_errors_and_truncation_and_wrappers():
    payload = load("payload_TBL_001.json")
    err = {"isError": True, "content": [{"type": "text", "text": "column not found: \"vatt\""}]}
    big = {"query": "q", "results": [{"time": "FY99", "versions": "Current Forecast"}] * 500}
    res, _ = detect_gaps(payload, [err, json.dumps(big)])
    assert res["summary"]["query_errors"] == 1
    assert any("truncated" in w for w in res["warnings"])
    assert res["summary"]["missing_cells"] == 13


def test_multi_dimension_exact_cover_and_leaf():
    payload = {
        "table_id": "T2", "module": "M", "sql_schema": "CORE_SCHEMA", "sql_table": "m.t",
        "select": ["amount", "headcount", "time", "versions", "entity"],
        "dimensions": [
            {"sql_column": "versions", "values": ["Actual"]},
            {"sql_column": "entity", "values": ["US", "UK", "Total Company"]},
            {"sql_column": "time", "values": ["2025-01-01T00:00:00", "2025-02-01T00:00:00", "Q1 FY25"]},
        ],
    }
    res0, st = detect_gaps(payload, [], leaf_dims=["entity"])   # round 0: plan initial queries
    sql0 = [q["sql"] for q in res0["suggested_queries"]]
    assert len(sql0) == 2 and all("\"entity_is_leaf\" = TRUE" in s for s in sql0)
    assert any("\"time_levels\" = 'month'" in s for s in sql0)
    assert any("\"time\" = 'Q1 FY25'" in s for s in sql0)
    # leaf query returns US/UK months + an unrequested entity; "Total Company" (parent) absent
    rows = [{"amount": 1, "headcount": 2, "time": t, "versions": "Actual", "entity": e}
            for e in ("US", "UK", "DE") for t in ("Jan 25", "Feb 25")]
    rows += [{"amount": 1, "headcount": 2, "time": "Q1 FY25", "versions": "Actual", "entity": "us"}]
    res, st = detect_gaps(payload, [wrap(rows)], st, leaf_dims=["entity"])
    covered = set()
    for p in res["next_payloads"]:
        ents = p["dimensions"][1]["values"]
        times = p["dimensions"][2]["values"]
        covered |= {(e, t) for e in ents for t in times}
    expected = {("Total Company", t) for t in payload["dimensions"][2]["values"]} | \
               {("UK", "Q1 FY25")}
    assert covered == expected, covered                     # exact: nothing already found re-requested
    assert res["missing"]["dimension_values_never_returned"] == {"entity": ["Total Company"]}
    # gap queries slice the multi-value dim by member equality (not leaf)
    assert all("\"entity_is_leaf\"" not in q["sql"] for q in res["suggested_queries"])


def test_unattributable_rows_warn():
    payload = {
        "table_id": "T3", "module": "M", "sql_schema": "S", "sql_table": "t",
        "select": ["amount"],
        "dimensions": [{"sql_column": "entity", "values": ["A", "B"]},
                       {"sql_column": "time", "values": ["FY25"]}],
    }
    res, _ = detect_gaps(payload, [wrap([{"amount": 1}])])
    assert any("SELECT every dimension column" in w for w in res["warnings"])
    # but an equality filter in the query string is enough to attribute the row
    res, _ = detect_gaps(payload, [wrap([{"amount": 1, "time": "FY25"}],
                                        query='SELECT "amount" FROM "t" WHERE "entity" = \'A\' LIMIT 500')])
    assert res["summary"]["complete_cells"] == 1


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
