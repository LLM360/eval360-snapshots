"""Import spreadsheet-style evaluation results into the Eval360 dashboard.

The workbook at /lustrefs/users/yuqi.wang/Eval360-V2/evaluation.xlsx contains
several sheets with a layout like:

    Group | Checkpoint | benchmark A | benchmark B | ...
    Metric| -          | accuracy    | pass@1      | ...
    mid1  | 5000       | 59.5        | 36.2        | ...

This script parses those sections without third-party Excel dependencies and
POSTs the numbers through the existing /api/ingest/eval-result endpoint.

Usage:
    python3 import_excel.py --xlsx /path/to/evaluation.xlsx --dashboard-url http://localhost:11003 --token TOKEN --apply

Preview only:
    python3 import_excel.py --xlsx /path/to/evaluation.xlsx
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}

COL_RE = re.compile(r"([A-Z]+)([0-9]+)")
HEADER_ALIASES = {
    ("group", "checkpoint"),
    ("model", "step"),
    ("family", "step"),
}


def _slug(value: str, fallback: str = "unnamed") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or fallback


def _column_index(col: str) -> int:
    value = 0
    for ch in col:
        value = value * 26 + ord(ch) - 64
    return value - 1


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    typ = cell.attrib.get("t")
    v = cell.find("m:v", NS)
    if typ == "s" and v is not None:
        return shared_strings[int(v.text or "0")]
    if typ == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//m:t", NS))
    if v is not None:
        return v.text or ""
    return ""


def _parse_number(value: str) -> float | None:
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_step(value: str) -> int | None:
    raw = str(value).strip()
    if not raw:
        return None
    num = _parse_number(raw)
    if num is not None:
        return int(num)
    match = re.search(r"(\d+(?:\.\d+)?)\s*k\b", raw, re.IGNORECASE)
    if match:
        return int(float(match.group(1)) * 1000)
    match = re.search(r"step[-_\s]*(\d+)", raw, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _normalise_score(value: float, mode: str) -> float:
    if mode == "raw":
        return value
    if mode == "percent":
        return value / 100.0
    if mode == "fraction":
        return value
    # Auto mode: most workbook metrics are percentages, but some agentic eval
    # sheets already use 0..1 proportions. Keep proportions as-is.
    if value > 1.0:
        return value / 100.0
    return value


@dataclass
class ColumnSpec:
    index: int
    dataset_name: str
    metric_name: str


@dataclass
class ParsedResult:
    sheet_name: str
    section_row: int
    section_title: str | None
    group_name: str
    checkpoint_label: str
    training_step: int | None
    columns: dict[str, dict[str, tuple[float, float, int]]] = field(default_factory=dict)

    @property
    def model_type(self) -> str:
        return "training" if self.training_step is not None else "baseline"


class Workbook:
    def __init__(self, path: Path):
        self.path = path
        self._zip = zipfile.ZipFile(path)
        self.shared_strings = self._load_shared_strings()
        self.sheets = self._load_sheet_paths()

    def close(self) -> None:
        self._zip.close()

    def _load_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            return []
        root = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
        return [
            "".join(t.text or "" for t in si.findall(".//m:t", NS))
            for si in root.findall("m:si", NS)
        ]

    def _load_sheet_paths(self) -> list[tuple[str, str]]:
        workbook = ET.fromstring(self._zip.read("xl/workbook.xml"))
        rels = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        out = []
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            name = sheet.attrib["name"]
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            out.append((name, "xl/" + relmap[rid].lstrip("/")))
        return out

    def rows(self, worksheet_path: str) -> list[tuple[int, list[str]]]:
        root = ET.fromstring(self._zip.read(worksheet_path))
        rows = []
        for row in root.findall("m:sheetData/m:row", NS):
            values: list[str] = []
            for cell in row.findall("m:c", NS):
                match = COL_RE.match(cell.attrib.get("r", ""))
                if not match:
                    continue
                idx = _column_index(match.group(1))
                while len(values) <= idx:
                    values.append("")
                values[idx] = _cell_text(cell, self.shared_strings).strip()
            rows.append((int(row.attrib.get("r", len(rows) + 1)), values))
        return rows


def _is_header(row: list[str]) -> bool:
    if len(row) < 2:
        return False
    return (row[0].strip().lower(), row[1].strip().lower()) in HEADER_ALIASES


def _section_title(rows: list[tuple[int, list[str]]], header_idx: int) -> str | None:
    if header_idx <= 0:
        return None
    prev = rows[header_idx - 1][1]
    nonempty = [v for v in prev if str(v).strip()]
    if len(nonempty) == 1 and not _is_header(prev):
        return nonempty[0]
    return None


def _metric_row(rows: list[tuple[int, list[str]]], header_idx: int) -> int | None:
    if header_idx + 1 >= len(rows):
        return None
    nxt = rows[header_idx + 1][1]
    if nxt and nxt[0].strip().lower() == "metric":
        return header_idx + 1
    return None


def _make_columns(header: list[str], metric: list[str] | None) -> list[ColumnSpec]:
    columns = []
    max_len = max(len(header), len(metric or []))
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for idx in range(2, max_len):
        dataset = header[idx].strip() if idx < len(header) else ""
        if not dataset:
            continue
        metric_name = metric[idx].strip() if metric and idx < len(metric) and metric[idx].strip() else "score"
        key = (dataset, metric_name)
        seen[key] += 1
        if seen[key] > 1:
            metric_name = f"{metric_name} #{seen[key]}"
        columns.append(ColumnSpec(idx, dataset, metric_name))
    return columns


def parse_workbook(path: Path, sheet_filter: set[str] | None = None, scale: str = "auto") -> list[ParsedResult]:
    wb = Workbook(path)
    results: list[ParsedResult] = []
    try:
        for sheet_name, worksheet_path in wb.sheets:
            if sheet_filter and sheet_name not in sheet_filter:
                continue
            rows = wb.rows(worksheet_path)
            header_indices = [i for i, (_, row) in enumerate(rows) if _is_header(row)]
            for section_pos, header_idx in enumerate(header_indices):
                header_row_num, header = rows[header_idx]
                metric_idx = _metric_row(rows, header_idx)
                metric = rows[metric_idx][1] if metric_idx is not None else None
                columns = _make_columns(header, metric)
                if not columns:
                    continue
                next_header_idx = header_indices[section_pos + 1] if section_pos + 1 < len(header_indices) else len(rows)
                first_data_idx = (metric_idx + 1) if metric_idx is not None else (header_idx + 1)
                title = _section_title(rows, header_idx)

                for _, row in rows[first_data_idx:next_header_idx]:
                    group_raw = row[0].strip() if len(row) > 0 else ""
                    checkpoint_raw = row[1].strip() if len(row) > 1 else ""
                    if not group_raw and not checkpoint_raw:
                        continue

                    numeric_cells: dict[str, dict[str, tuple[float, float, int]]] = defaultdict(dict)
                    for col in columns:
                        raw = row[col.index].strip() if col.index < len(row) else ""
                        value = _parse_number(raw)
                        if value is None:
                            continue
                        numeric_cells[col.dataset_name][col.metric_name] = (
                            _normalise_score(value, scale),
                            value,
                            col.index,
                        )
                    if not numeric_cells:
                        continue

                    step = _parse_step(checkpoint_raw)
                    group_name = group_raw or checkpoint_raw
                    results.append(ParsedResult(
                        sheet_name=sheet_name,
                        section_row=header_row_num,
                        section_title=title,
                        group_name=group_name,
                        checkpoint_label=checkpoint_raw,
                        training_step=step,
                        columns=dict(numeric_cells),
                    ))
    finally:
        wb.close()
    return results


def _model_id(prefix: str, result: ParsedResult) -> str:
    return "__".join([
        _slug(prefix, "excel"),
        _slug(result.sheet_name, "sheet"),
        f"r{result.section_row}",
        _slug(result.group_name, "group"),
    ])


def _checkpoint_id(model_id: str, result: ParsedResult) -> str:
    if result.training_step is not None:
        return f"{model_id}__step-{result.training_step}"
    return f"{model_id}__{_slug(result.checkpoint_label or result.group_name, 'baseline')}"


def _eval_run_id(checkpoint_id: str, dataset_name: str) -> str:
    return f"{checkpoint_id}__{_slug(dataset_name, 'dataset')}"[:240]


def build_payloads(
    parsed: list[ParsedResult],
    workbook_name: str,
    owner: str,
    prefix: str,
) -> list[dict[str, Any]]:
    payloads = []
    for result in parsed:
        model_id = _model_id(prefix, result)
        checkpoint_id = _checkpoint_id(model_id, result)
        for dataset_name, metrics in result.columns.items():
            metric_values = {name: value for name, (value, _original, _col) in metrics.items()}
            original_values = {name: original for name, (_value, original, _col) in metrics.items()}
            col_indices = {name: col for name, (_value, _original, col) in metrics.items()}
            primary_metric = next(iter(metric_values))
            payloads.append({
                "model_id": model_id,
                "display_name": result.group_name,
                "model_type": result.model_type,
                "owner": owner,
                "checkpoint_id": checkpoint_id,
                "training_step": result.training_step,
                "checkpoint_path": result.checkpoint_label if result.training_step is None else None,
                "dataset_name": dataset_name,
                "metrics": metric_values,
                "primary_metric": primary_metric,
                "eval_run_id": _eval_run_id(checkpoint_id, dataset_name),
                "training_run": f"excel:{result.sheet_name}",
                "recipe_tags": ["excel-import", _slug(result.sheet_name)],
                "eval_config": {
                    "excel": {
                        "workbook": workbook_name,
                        "sheet_name": result.sheet_name,
                        "section_row": result.section_row,
                        "section_title": result.section_title,
                        "group": result.group_name,
                        "checkpoint": result.checkpoint_label,
                        "values": original_values,
                        "col_indices": col_indices,
                    }
                },
                "metadata": {
                    "imported_from": workbook_name,
                    "sheet_name": result.sheet_name,
                    "section_row": result.section_row,
                    "section_title": result.section_title,
                },
            })
    return payloads


def post_payload(url: str, token: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ingest/eval-result",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}: {resp.read().decode('utf-8', 'replace')}")


def summarize(parsed: list[ParsedResult], payloads: list[dict[str, Any]]) -> None:
    by_sheet: dict[str, dict[str, int]] = defaultdict(lambda: {"rows": 0, "payloads": 0, "metrics": 0})
    for result in parsed:
        by_sheet[result.sheet_name]["rows"] += 1
        by_sheet[result.sheet_name]["metrics"] += sum(len(metrics) for metrics in result.columns.values())
    for payload in payloads:
        excel = payload["eval_config"]["excel"]
        by_sheet[excel["sheet_name"]]["payloads"] += 1

    print("Parsed workbook sections:")
    for sheet_name, counts in sorted(by_sheet.items()):
        print(
            f"  {sheet_name}: {counts['rows']} checkpoint rows, "
            f"{counts['metrics']} metric cells, {counts['payloads']} ingest posts"
        )
    print(f"Total: {len(parsed)} checkpoint rows, {sum(len(p['metrics']) for p in payloads)} metric cells")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Excel evaluation numbers into Eval360")
    parser.add_argument("--xlsx", default="/lustrefs/users/yuqi.wang/Eval360-V2/evaluation.xlsx")
    parser.add_argument("--dashboard-url", default="http://localhost:11003")
    parser.add_argument("--token", default="")
    parser.add_argument("--owner", default="excel")
    parser.add_argument("--model-prefix", default="excel")
    parser.add_argument("--sheets", default="", help="Comma-separated sheet names to import. Default: all detected sections.")
    parser.add_argument("--scale", choices=["auto", "percent", "fraction", "raw"], default="auto")
    parser.add_argument("--apply", action="store_true", help="Actually POST to the dashboard. Default is preview only.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Optional delay between POSTs.")
    args = parser.parse_args()

    xlsx = Path(args.xlsx)
    if not xlsx.exists():
        print(f"error: {xlsx} does not exist", file=sys.stderr)
        return 1

    sheet_filter = {s.strip() for s in args.sheets.split(",") if s.strip()} or None
    parsed = parse_workbook(xlsx, sheet_filter=sheet_filter, scale=args.scale)
    payloads = build_payloads(parsed, xlsx.name, args.owner, args.model_prefix)
    summarize(parsed, payloads)

    if not args.apply:
        print("\nPreview only. Add --apply --token TOKEN to upload.")
        if payloads:
            sample = payloads[0].copy()
            sample["metrics"] = dict(list(sample["metrics"].items())[:3])
            print("\nSample payload:")
            print(json.dumps(sample, indent=2)[:3000])
        return 0

    token = args.token
    if not token:
        print("error: --token is required with --apply", file=sys.stderr)
        return 1

    ok = 0
    for idx, payload in enumerate(payloads, 1):
        try:
            post_payload(args.dashboard_url, token, payload)
            ok += 1
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            print(f"FAIL {idx}/{len(payloads)} {payload['checkpoint_id']} {payload['dataset_name']}: HTTP {exc.code} {detail}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"FAIL {idx}/{len(payloads)} {payload['checkpoint_id']} {payload['dataset_name']}: {exc}", file=sys.stderr)
            return 1
        if args.sleep:
            time.sleep(args.sleep)
        if idx % 100 == 0:
            print(f"Uploaded {idx}/{len(payloads)}")
    print(f"Uploaded {ok}/{len(payloads)} payloads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
