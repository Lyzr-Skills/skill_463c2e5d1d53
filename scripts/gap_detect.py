#!/usr/bin/env python3
"""
Anaplan gap detector.

Compares a retrieval payload (table_id / module / select / dimensions) with the
verbatim outputs of `sql_query` tool calls and works out what is still missing:

  * cells (combinations of requested dimension values) that never came back
  * value columns (line items from `select`) that are absent for some cells

It returns follow-up payloads with the SAME structure as the input payload, each
covering only missing data (an exact rectangle cover, no over-fetching of cells),
plus ready-to-run SQL that follows the agent's Global SQL rules.

Run it after every batch of sql_query calls, feed the next payloads back in, and
repeat until status is "complete" (or "exhausted" = what is left has been
requested explicitly and Anaplan returned nothing for it).

Usage (CLI):
    python gap_detect.py --payload payload.json --outputs out/*.json --state state.json
    python gap_detect.py --payload payload.json --state state.json          # round 0: plan initial queries
    cat bundle.json | python gap_detect.py --state state.json               # {"payload":..., "outputs":[...]}

Usage (library, e.g. from the orchestrator that collects tool outputs):
    from gap_detect import detect_gaps
    result, new_state = detect_gaps(payload, outputs, state)

Standard library only.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import re
import sys
from collections import defaultdict

SQL_LIMIT = 500
HELPER_SUFFIXES = ("_is_leaf", "_levels")
TIME_LEVELS = ("month", "quarter", "year")
STATE_VERSION = 1

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_LOOKUP = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                         "august", "september", "october", "november", "december"], 1):
    MONTH_LOOKUP[_m] = _i
    MONTH_LOOKUP[_m[:3]] = _i
MONTH_LOOKUP["sept"] = 9


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
def norm_text(value) -> str:
    """Trim, collapse whitespace, lower-case. Used for member and column matching."""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _yy(year_str: str) -> int:
    return int(year_str) % 100


def parse_time(value):
    """
    Classify a time label and return (level, canonical_key, model_label).

    Accepts ISO dates ("2027-02-01T00:00:00"), model months ("Feb 27", "Feb-27",
    "February 2027"), quarters ("Q2 FY25", "FY25 Q2") and years ("FY25", "FY2025").
    canonical_key is identical for equivalent spellings, so requested values and
    returned values match even if the formats differ.
    """
    s = str(value).strip()
    low = s.lower()

    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(?:[t ].*)?$", low)
    if m and 1 <= int(m.group(2)) <= 12:
        y, mo = _yy(m.group(1)), int(m.group(2))
        return "month", f"M:{y:02d}-{mo:02d}", f"{MONTH_ABBR[mo]} {y:02d}"

    m = re.match(r"^q\s*([1-4])\s*[-_ ]?\s*fy\s*'?(\d{2}|\d{4})$", low) or \
        re.match(r"^fy\s*'?(?P<y>\d{2}|\d{4})\s*[-_ ]?\s*q\s*(?P<q>[1-4])$", low)
    if m:
        if "q" in m.groupdict() and m.groupdict().get("q"):
            q, y = int(m.group("q")), _yy(m.group("y"))
        else:
            q, y = int(m.group(1)), _yy(m.group(2))
        return "quarter", f"Q:{y:02d}-{q}", f"Q{q} FY{y:02d}"

    m = re.match(r"^fy\s*'?(\d{2}|\d{4})$", low)
    if m:
        y = _yy(m.group(1))
        return "year", f"Y:{y:02d}", f"FY{y:02d}"

    m = re.match(r"^([a-z]{3,9})\.?[\s\-_/']*(\d{2}|\d{4})$", low)
    if m and m.group(1) in MONTH_LOOKUP:
        mo, y = MONTH_LOOKUP[m.group(1)], _yy(m.group(2))
        return "month", f"M:{y:02d}-{mo:02d}", f"{MONTH_ABBR[mo]} {y:02d}"

    return "unknown", "X:" + norm_text(s), s


def is_helper(col: str) -> bool:
    c = norm_text(col)
    return c.endswith(HELPER_SUFFIXES) or c == "time_levels"


def q_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def q_lit(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------- #
# Payload model
# --------------------------------------------------------------------------- #
class PayloadSpec:
    def __init__(self, payload: dict, time_column: str | None = None):
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        for key in ("module", "sql_table", "select", "dimensions"):
            if key not in payload:
                raise ValueError(f"payload is missing '{key}'")
        self.payload = payload
        self.table_id = payload.get("table_id", "")
        self.select = list(payload["select"])
        self.dims = []           # list of sql_column names, payload order
        self.dim_members = {}    # dim -> list of (original, canonical), deduped, ordered
        self.canon_to_orig = {}  # dim -> {canonical: original}

        raw_dims = payload["dimensions"] or []
        explicit_time = norm_text(time_column) if time_column else None
        self.time_dim = None
        for d in raw_dims:
            col = d["sql_column"]
            if explicit_time and norm_text(col) == explicit_time:
                self.time_dim = col
        if self.time_dim is None and not explicit_time:
            for d in raw_dims:
                if norm_text(d["sql_column"]) == "time":
                    self.time_dim = d["sql_column"]
            if self.time_dim is None:  # fall back: a dimension whose values all parse as time
                for d in raw_dims:
                    vals = d.get("values") or []
                    if vals and all(parse_time(v)[0] != "unknown" for v in vals):
                        self.time_dim = d["sql_column"]
                        break

        for d in raw_dims:
            col = d["sql_column"]
            self.dims.append(col)
            seen, members = set(), []
            for v in d.get("values") or []:
                c = self.canon(col, v)
                if c not in seen:
                    seen.add(c)
                    members.append((v, c))
            if not members:
                raise ValueError(f"dimension '{col}' has no values")
            self.dim_members[col] = members
            self.canon_to_orig[col] = {c: o for o, c in members}

        dim_norm = {norm_text(c) for c in self.dims}
        self.value_cols = []
        for c in self.select:
            if norm_text(c) in dim_norm or is_helper(c):
                continue
            if c not in self.value_cols:
                self.value_cols.append(c)

    def canon(self, dim: str, value) -> str:
        if value is None:
            return "∅"
        if dim == self.time_dim:
            return parse_time(value)[1]
        return norm_text(value)

    def all_cells(self):
        return itertools.product(*[[c for _, c in self.dim_members[d]] for d in self.dims])

    def cell_to_dict(self, cell):
        return {d: self.canon_to_orig[d][c] for d, c in zip(self.dims, cell)}


# --------------------------------------------------------------------------- #
# Tool output extraction (verbatim, any wrapping)
# --------------------------------------------------------------------------- #
def _try_json(text: str):
    t = text.strip()
    if not t or t[0] not in "[{\"":
        return None
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        return None


def extract_query_results(obj, source="output", _depth=0):
    """
    Walk whatever the tool returned (MCP content blocks, JSON strings, raw dicts,
    lists of several outputs) and yield
        {"source", "query", "results" | None, "error" | None}
    for every sql_query result found.
    """
    if _depth > 12:
        return
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8", "replace")
    if isinstance(obj, str):
        parsed = _try_json(obj)
        if parsed is not None:
            yield from extract_query_results(parsed, source, _depth + 1)
        elif re.search(r"\b(error|exception|failed|invalid)\b", obj, re.I):
            yield {"source": source, "query": None, "results": None, "error": obj.strip()[:500]}
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from extract_query_results(item, f"{source}[{i}]", _depth + 1)
        return
    if not isinstance(obj, dict):
        return

    if obj.get("isError") or obj.get("is_error"):
        text = json.dumps(obj.get("content", obj))[:500]
        yield {"source": source, "query": obj.get("query"), "results": None, "error": text}
        return
    for key in ("results", "rows", "data"):
        if isinstance(obj.get(key), list) and (key == "results" or "query" in obj):
            yield {"source": source, "query": obj.get("query"), "results": obj[key],
                   "error": obj.get("error")}
            return
    if obj.get("error") and not any(k in obj for k in ("text", "content")):
        yield {"source": source, "query": obj.get("query"), "results": None,
               "error": str(obj["error"])[:500]}
        return
    for key in ("text", "content", "output", "result", "tool_result", "outputs"):
        if key in obj:
            yield from extract_query_results(obj[key], f"{source}.{key}", _depth + 1)


def parse_equality_filters(query: str | None) -> dict:
    """'"col" = 'value'' pairs from a SQL string -> {normalised col: value}."""
    out = {}
    if not query:
        return out
    for col, val in re.findall(r'"((?:[^"]|"")+)"\s*=\s*\'((?:[^\']|\'\')*)\'', query):
        out[norm_text(col.replace('""', '"'))] = val.replace("''", "'")
    return out


# --------------------------------------------------------------------------- #
# Rectangle cover: exact partition of missing cells into payload-shaped blocks
# --------------------------------------------------------------------------- #
def rectangle_cover(cells, ndims, merge_order):
    blocks = {tuple(frozenset([c[i]]) for i in range(ndims)) for c in cells}
    changed = True
    while changed:
        changed = False
        for d in merge_order:
            groups = defaultdict(set)
            for b in blocks:
                groups[b[:d] + b[d + 1:]] |= b[d]
            merged = {k[:d] + (frozenset(v),) + k[d:] for k, v in groups.items()}
            if len(merged) != len(blocks):
                changed = True
            blocks = merged
    return blocks


# --------------------------------------------------------------------------- #
# SQL planning for a payload
# --------------------------------------------------------------------------- #
def plan_queries(spec: PayloadSpec, sub_payload: dict, exact: bool, leaf_dims: set):
    """
    Build sql_query statements that retrieve sub_payload, obeying the agent rules:
    LIMIT 500, no IN/OR/DISTINCT/GROUP BY, every dimension sliced, identifiers quoted.

    exact=False: time sliced by "time_levels" when >1 value of a level is needed;
                 multi-value dims in leaf_dims sliced by "<dim>_is_leaf" = TRUE.
    exact=True : every dimension sliced by equality (used for retries).
    """
    select_sql = ", ".join(q_ident(c) for c in sub_payload["select"])
    base = f"SELECT {select_sql} FROM {q_ident(spec.payload['sql_table'])}"

    per_dim_slices = []
    for d in sub_payload["dimensions"]:
        col, vals = d["sql_column"], d["values"]
        if col == spec.time_dim:
            by_level = defaultdict(list)
            for v in vals:
                lvl, _, label = parse_time(v)
                by_level[lvl].append(label)
            slices = []
            for lvl in list(TIME_LEVELS) + ["unknown"]:
                labels = by_level.get(lvl, [])
                if not labels:
                    continue
                if not exact and lvl in TIME_LEVELS and len(labels) > 1:
                    slices.append(f"{q_ident(col + '_levels')} = {q_lit(lvl)}")
                else:
                    slices.extend(f"{q_ident(col)} = {q_lit(l)}" for l in labels)
            per_dim_slices.append(slices)
        elif len(vals) == 1:
            per_dim_slices.append([f"{q_ident(col)} = {q_lit(vals[0])}"])
        elif not exact and norm_text(col) in leaf_dims:
            per_dim_slices.append([f"{q_ident(col + '_is_leaf')} = TRUE"])
        else:
            per_dim_slices.append([f"{q_ident(col)} = {q_lit(v)}" for v in vals])

    queries = []
    for combo in itertools.product(*per_dim_slices):
        where = " AND ".join(combo)
        queries.append(f"{base} WHERE {where} LIMIT {SQL_LIMIT}" if where else f"{base} LIMIT {SQL_LIMIT}")
    return queries


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def _ck(cell) -> str:
    return json.dumps(list(cell), ensure_ascii=False)


def detect_gaps(payload, outputs=None, state=None, *, max_attempts=2, null_is_missing=False,
                time_column=None, leaf_dims=None, max_list=200):
    """
    payload : the input payload dict (original request).
    outputs : list of verbatim sql_query outputs (any wrapping). May be cumulative
              or only the newest batch; found data is accumulated in `state`.
    state   : dict from the previous call (or None for the first call).
    Returns (result_dict, new_state).
    """
    spec = PayloadSpec(payload, time_column)
    leaf_dims = {norm_text(d) for d in (leaf_dims or [])}
    outputs = outputs or []

    state = dict(state or {})
    if state and state.get("table_id") not in (None, spec.table_id):
        raise ValueError(f"state belongs to table_id {state.get('table_id')!r}, not {spec.table_id!r}")
    found = {k: set(v) for k, v in (state.get("found") or {}).items()}
    attempts = dict(state.get("attempts") or {})
    no_data = {k: list(v) for k, v in (state.get("no_data") or {}).items()}
    pending = set(state.get("pending") or [])
    round_no = int(state.get("round", 0)) + 1

    requested_cells = {_ck(c): c for c in spec.all_cells()}
    dim_index = {norm_text(d): i for i, d in enumerate(spec.dims)}
    value_norm = {norm_text(c): c for c in spec.value_cols}

    warnings, errors = [], []
    stats = defaultdict(int)

    # ---- read outputs ------------------------------------------------------ #
    for qi, qres in enumerate(itertools.chain.from_iterable(
            extract_query_results(o, f"output[{i}]") for i, o in enumerate(outputs))):
        stats["query_results"] += 1
        if qres["error"] and not qres["results"]:
            errors.append({"source": qres["source"], "query": qres["query"], "error": qres["error"]})
            continue
        rows = qres["results"] or []
        if len(rows) >= SQL_LIMIT:
            warnings.append(f"{qres['source']}: returned {len(rows)} rows (LIMIT {SQL_LIMIT}) - "
                            f"result may be truncated; missing cells will be re-requested with "
                            f"narrower filters. Query: {(qres['query'] or '')[:200]}")
        filters = parse_equality_filters(qres["query"])
        unattributable = 0
        for row in rows:
            stats["rows_read"] += 1
            if not isinstance(row, dict):
                continue
            keymap = {norm_text(k): k for k in row}
            cell = []
            ok = True
            for d in spec.dims:
                nd = norm_text(d)
                if nd in keymap:
                    cell.append(spec.canon(d, row[keymap[nd]]))
                elif nd in filters:
                    cell.append(spec.canon(d, filters[nd]))
                elif len(spec.dim_members[d]) == 1:
                    cell.append(spec.dim_members[d][0][1])
                else:
                    ok = False
                    break
            if not ok:
                unattributable += 1
                continue
            key = _ck(cell)
            if key not in requested_cells:
                stats["rows_outside_request"] += 1
                continue
            stats["rows_matched"] += 1
            present = found.setdefault(key, set())
            for nk, orig_k in keymap.items():
                if nk in value_norm and not (null_is_missing and row[orig_k] is None):
                    present.add(value_norm[nk])
        if unattributable:
            warnings.append(f"{qres['source']}: {unattributable} row(s) skipped - a multi-value "
                            f"dimension column was not in the result and not fixed by an equality "
                            f"filter. Always SELECT every dimension column.")

    # ---- compute what is missing ------------------------------------------ #
    value_set = set(spec.value_cols)
    missing = {}  # key -> sorted list of missing value columns (or ["<row>"] if no value cols)
    for key in requested_cells:
        if key in no_data:
            continue
        if spec.value_cols:
            miss = value_set - found.get(key, set())
            if miss:
                missing[key] = [c for c in spec.value_cols if c in miss]
        elif key not in found:
            missing[key] = []

    # ---- attempts / exhaustion -------------------------------------------- #
    for key in list(missing):
        if key in pending:
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] >= max_attempts:
                no_data[key] = missing.pop(key)
    new_pending = sorted(missing)

    # ---- build follow-up payloads ----------------------------------------- #
    n = len(spec.dims)
    order = [i for i, d in enumerate(spec.dims) if d != spec.time_dim] + \
            [i for i, d in enumerate(spec.dims) if d == spec.time_dim]
    # Cells are grouped by their exact set of missing columns, then each group is
    # partitioned into rectangles -> every follow-up payload says precisely which
    # line items are missing for precisely which members.
    by_cols = defaultdict(list)
    for k, cols in missing.items():
        by_cols[tuple(cols)].append(requested_cells[k])
    member_pos = {d: {c: j for j, (_, c) in enumerate(spec.dim_members[d])} for d in spec.dims}

    def block_sort_key(item):
        b, cols = item
        return ([min(member_pos[spec.dims[i]][c] for c in b[i]) for i in range(n)],
                [spec.value_cols.index(c) for c in cols])

    blocks = []
    for cols, cells_ in by_cols.items():
        rects = rectangle_cover(cells_, n, order) if n else {()}
        blocks.extend((r, list(cols)) for r in rects)

    next_payloads, suggested = [], []
    for bi, (block, cols) in enumerate(sorted(blocks, key=block_sort_key)):
        cells = [_ck(c) for c in itertools.product(*block)]
        # select: original order, keep dimension columns, drop value columns already retrieved
        select, seen = [], set()
        dim_norm = {norm_text(d) for d in spec.dims}
        for c in spec.select:
            nc = norm_text(c)
            if (nc in dim_norm or c in cols or is_helper(c)) and nc not in seen:
                select.append(c)
                seen.add(nc)
        for d in spec.dims:
            if norm_text(d) not in seen:
                select.append(d)
                seen.add(norm_text(d))
        dims_out = []
        for i, d in enumerate(spec.dims):
            ordered = [o for o, c in spec.dim_members[d] if c in block[i]]
            dims_out.append({"sql_column": d, "values": ordered})
        sub = {k: v for k, v in spec.payload.items() if k not in ("select", "dimensions")}
        sub["select"] = select
        sub["dimensions"] = dims_out
        sub["gap_batch"] = f"r{round_no}-{bi + 1}"
        next_payloads.append(sub)

        exact = max((attempts.get(k, 0) for k in cells), default=0) >= 1
        for sql in plan_queries(spec, sub, exact=exact, leaf_dims=leaf_dims):
            suggested.append({"payload_index": bi, "gap_batch": sub["gap_batch"], "sql": sql})

    # ---- summaries --------------------------------------------------------- #
    covered_cols = set().union(*found.values()) if found else set()
    cols_never = [c for c in spec.value_cols if c not in covered_cols]
    dim_never = {}
    for i, d in enumerate(spec.dims):
        got = {json.loads(k)[i] for k in found if k in requested_cells}
        never = [o for o, c in spec.dim_members[d] if c not in got]
        if never:
            dim_never[d] = never

    if missing:
        status = "incomplete"
    elif no_data:
        status = "exhausted"
    else:
        status = "complete"

    complete_cells = len(requested_cells) - len(missing) - len(no_data)
    no_data_list = [{"dimensions": spec.cell_to_dict(requested_cells[k]), "missing_columns": v}
                    for k, v in no_data.items() if k in requested_cells]

    result = {
        "status": status,
        "table_id": spec.table_id,
        "round": round_no,
        "summary": {
            "requested_cells": len(requested_cells),
            "complete_cells": complete_cells,
            "missing_cells": len(missing),
            "partial_cells": sum(1 for k in missing if k in found),
            "no_data_cells": len(no_data_list),
            "value_columns": len(spec.value_cols),
            "query_results_read": stats["query_results"],
            "query_errors": len(errors),
            "rows_read": stats["rows_read"],
            "rows_matched": stats["rows_matched"],
            "rows_outside_request": stats["rows_outside_request"],
            "next_payloads": len(next_payloads),
            "suggested_queries": len(suggested),
        },
        "missing": {
            "columns_never_returned": cols_never,
            "dimension_values_never_returned": dim_never,
        },
        "next_payloads": next_payloads,
        "suggested_queries": suggested,
        "no_data": no_data_list[:max_list],
        "errors": errors[:max_list],
        "warnings": warnings[:max_list],
    }
    if len(no_data_list) > max_list:
        result["no_data_truncated"] = len(no_data_list) - max_list

    new_state = {
        "version": STATE_VERSION,
        "table_id": spec.table_id,
        "round": round_no,
        "found": {k: sorted(v) for k, v in found.items()},
        "attempts": attempts,
        "pending": new_pending,
        "no_data": no_data,
    }
    return result, new_state


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_json_arg(arg: str):
    if arg.strip().startswith(("{", "[")):
        return json.loads(arg)
    with open(arg, encoding="utf-8") as fh:
        return json.load(fh)


def _load_output_file(path: str):
    """Tool outputs are kept verbatim; non-JSON text is passed through as a string."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except ValueError:
        return text


def _expand(paths):
    files = []
    for p in paths or []:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*"))))
        else:
            matched = sorted(glob.glob(p))
            files.extend(matched if matched else [p])
    return [f for f in files if os.path.isfile(f)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Detect missing Anaplan data and plan follow-up queries.")
    ap.add_argument("--payload", help="payload JSON file or inline JSON. If omitted, read a "
                                      "bundle {payload, outputs} from stdin.")
    ap.add_argument("--outputs", nargs="*", default=[],
                    help="files/dirs/globs with verbatim sql_query outputs")
    ap.add_argument("--state", help="state file (read if present, always written back)")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="explicit re-requests of a cell before it is declared no-data (default 2)")
    ap.add_argument("--null-is-missing", action="store_true",
                    help="treat null values as missing (default: a returned null counts as received)")
    ap.add_argument("--time-column", help="time dimension sql_column (default: auto-detect 'time')")
    ap.add_argument("--leaf-dims", default="",
                    help="comma-separated dims with a <dim>_is_leaf column in sql_schema; "
                         "used for first-pass queries on multi-value dims")
    ap.add_argument("--out", help="write the result JSON here as well as stdout")
    ap.add_argument("--compact", action="store_true", help="single-line JSON")
    args = ap.parse_args(argv)

    try:
        outputs = [_load_output_file(f) for f in _expand(args.outputs)]
        if args.payload:
            payload = _load_json_arg(args.payload)
        else:
            bundle = json.load(sys.stdin)
            payload = bundle["payload"]
            outputs = list(bundle.get("outputs") or []) + outputs
        state = None
        if args.state and os.path.exists(args.state):
            with open(args.state, encoding="utf-8") as fh:
                state = json.load(fh)
        result, new_state = detect_gaps(
            payload, outputs, state,
            max_attempts=args.max_attempts, null_is_missing=args.null_is_missing,
            time_column=args.time_column,
            leaf_dims=[d for d in args.leaf_dims.split(",") if d.strip()],
        )
    except Exception as exc:  # report as JSON so an agent can act on it
        print(json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"}))
        return 2

    if args.state:
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(new_state, fh)
        os.replace(tmp, args.state)
    text = json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
