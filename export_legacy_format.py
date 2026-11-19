"""Export a saved snapshot with the historical ICLR/ICML paper-table schema.

No network access. Original snapshot files are read-only inputs. Every source
column is retained under a documented destination name; old-only fields are
blank when unavailable. CSV and string-valued Zstd Parquet match earlier runs.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


BASE = ["link", "title", "authors", "keywords", "abstract", "primary_area",
        "submission_number", "type", "Meta Review", "Meta Reviewer", "General Response"]
OLD_SCORES = ["Soundness", "Presentation", "Contribution", "Rating", "Confidence"]
NEW_SCORES = {"soundness", "presentation", "contribution", "rating", "confidence",
              "informativeness", "overall_recommendation"}
REVIEW_METADATA = {
    "full_review": "", "reviewer_alias": None, "reviewer": "Reviewer Signature",
    "review_id": "ID", "review_link": "Link", "captured_at_utc": "Captured At UTC",
    "cdate": "Created Timestamp", "mdate": "Modified Timestamp",
    "tcdate": "True Created Timestamp", "tmdate": "True Modified Timestamp",
    "replyto": "Reply To", "readers": "Readers", "invitations": "Invitations",
    "content_json": "Content JSON",
}


def pretty(key):
    return key.replace("_", " ").title()


def unwrap(content, key, default=""):
    v = (content or {}).get(key, default)
    return v.get("value", default) if isinstance(v, dict) else v


def text(content):
    return "\n\n".join(pretty(key) + ":\n" + str(unwrap(content, key)) for key in content)


def make_mapping(source_fields):
    mapping = {}
    for name in source_fields:
        match = re.fullmatch(r"Review (\d+) (.+)", name)
        if match:
            i, key = match.groups()
            prefix = f"Review {i}"
            if key == "reviewer_alias":
                dest = f"Reviewer {i}"
            elif key == "full_review":
                dest = prefix
            elif key.startswith("content."):
                dest = prefix + " " + pretty(key[8:])
            elif key.startswith("score."):
                field = key[6:]
                dest = prefix + " " + (pretty(field) + " Numeric" if field in NEW_SCORES
                                         else "Parsed Score " + pretty(field))
            else:
                dest = prefix + " " + REVIEW_METADATA.get(key, pretty(key))
        elif name == "authors":
            dest = "Source Authors"
        elif name.startswith("content."):
            dest = "Source " + pretty(name[8:])
        else:
            dest = name
        if dest in mapping.values():
            raise ValueError("Destination column collision: " + dest)
        mapping[name] = dest
    return mapping


def make_headers(mapping):
    # Keep all five historical reviewer blocks even for a small smoke-test run.
    maximum = max(5, max((int(m.group(1)) for col in mapping
                          if (m := re.match(r"Review (\d+) ", col))), default=0))
    headers = list(BASE)
    for i in range(1, maximum + 1):
        headers += [f"Review {i}", f"Reviewer {i}", f"Rebuttal {i}"]
        headers += [f"Review {i} {key}" for key in OLD_SCORES]
    # ICML 2025 used this additional score name. Retain it for cross-year appends.
    headers += [f"Review {i} Overall Recommendation" for i in range(1, maximum + 1)]
    for dest in mapping.values():
        if dest not in headers:
            headers.append(dest)
    for i in range(1, maximum + 1):
        headers += [f"Review {i} Comments"]
    headers += ["Public Replies JSON"]
    if len(headers) != len(set(headers)):
        raise ValueError("Duplicate output headers")
    return headers, maximum


def extra_replies(snapshot):
    extras = defaultdict(list)
    counts = Counter()
    with (snapshot / "public_replies.jsonl").open(encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            note = record["note"]
            if any(i.endswith("/-/Official_Review") for i in note.get("invitations", [])):
                continue
            extras[record["paper_id"]].append(note)
            counts.update(i.rsplit("/-/", 1)[-1] for i in note.get("invitations", []))
    print(f"Retaining {sum(map(len, extras.values()))} additional public replies: {dict(counts)}", flush=True)
    return extras


def author_string(names, ids):
    names = names if isinstance(names, list) else [names]
    ids = ids if isinstance(ids, list) else [ids]
    return "; ".join(str(name) + (f" (https://openreview.net/profile?id={ids[i]})"
                       if i < len(ids) and str(ids[i]).startswith("~") else "")
                     for i, name in enumerate(names))


def json_cell(row, name, default):
    value = row.get(name, "")
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def convert_row(source, mapping, maximum, extras):
    row = {dest: source.get(name, "") for name, dest in mapping.items()}
    row["authors"] = author_string(json_cell(source, "authors", []), json_cell(source, "authorids", []))
    keywords = json_cell(source, "content.keywords", [])
    row["keywords"] = ", ".join(map(str, keywords)) if isinstance(keywords, list) else str(keywords)
    row["primary_area"] = source.get("content.primary_area", "")
    row["type"] = "Active Submission"
    for name in ("Meta Review", "Meta Reviewer", "General Response"):
        row[name] = ""
    for i in range(1, maximum + 1):
        row[f"Rebuttal {i}"] = ""
        row[f"Review {i} Comments"] = ""
    notes = extras.get(source["paper_id"], [])
    row["Public Replies JSON"] = json.dumps(notes, ensure_ascii=False) if notes else ""
    by_id = {note["id"]: note for note in notes}
    targets = {source.get(f"Review {i} review_id"): i for i in range(1, maximum + 1)
               if source.get(f"Review {i} review_id")}
    for note in notes:
        sig = (note.get("signatures") or [""])[0]
        content = note.get("content", {})
        if any(inv.endswith("/-/Meta_Review") for inv in note.get("invitations", [])):
            row["Meta Review"] += ("\n\n---\n\n" if row["Meta Review"] else "") + text(content)
            row["Meta Reviewer"] += ("; " if row["Meta Reviewer"] else "") + sig.rsplit("/", 1)[-1].replace("_", " ")
            continue
        target = note.get("replyto")
        for _ in range(12):
            if target in targets or target not in by_id:
                break
            target = by_id[target].get("replyto")
        if sig.endswith("/Authors"):
            prose = "\n\n".join(str(unwrap(content, key)) for key in ("comment", "rebuttal", "response") if unwrap(content, key))
            prose = "\n".join(line for line in prose.splitlines() if not line.lstrip().startswith(">"))
            prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
            key = f"Rebuttal {targets[target]}" if target in targets else "General Response"
        else:
            prose = text(content)
            key = f"Review {targets[target]} Comments" if target in targets else None
        if key and prose:
            row[key] += ("\n\n---\n\n" if row[key] else "") + prose
    return row


def validate_files(source_path, parquet_path, csv_path, mapping, headers):
    original = pq.ParquetFile(source_path)
    result = pq.ParquetFile(parquet_path)
    count = 0
    for left, right in zip(original.iter_batches(batch_size=500), result.iter_batches(batch_size=500), strict=True):
        if left.num_rows != right.num_rows:
            raise ValueError("Source/output batch row count mismatch")
        for source, dest in mapping.items():
            if not left.column(left.schema.get_field_index(source)).equals(right.column(right.schema.get_field_index(dest))):
                raise ValueError("Changed or missing source values: " + source)
        count += left.num_rows
    # The standard reader handles the captured reviews' embedded CR/LF and
    # quoting without block-boundary parsing failures on unusually long fields.
    csv.field_size_limit(2 ** 31 - 1)
    csv_count = 0
    with csv_path.open(encoding="utf-8", newline="") as f:
        csv_reader = csv.DictReader(f, strict=True)
        if csv_reader.fieldnames != headers:
            raise ValueError("CSV column names/order differ from Parquet")
        for batch in result.iter_batches(batch_size=500):
            for expected in batch.to_pylist():
                actual = next(csv_reader)
                if actual != expected:
                    changed = [key for key in headers if actual.get(key) != expected.get(key)]
                    raise ValueError("CSV/Parquet values differ for paper " + expected["paper_id"] + ": " + str(changed))
                csv_count += 1
        if next(csv_reader, None) is not None:
            raise ValueError("Extra CSV rows")
    if count != original.metadata.num_rows or csv_count != count:
        raise ValueError("Output count differs from source")
    return count


def export(snapshot, output_dir, finish_existing=False):
    source_path = snapshot / "papers.parquet"
    source = pq.ParquetFile(source_path)
    mapping = make_mapping(source.schema_arrow.names)
    headers, maximum = make_headers(mapping)
    extras = extra_replies(snapshot)
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = "ICLR_2027_Conference_Active_Submissions_papers"
    csv_path, parquet_path = output_dir / (basename + ".csv"), output_dir / (basename + ".parquet")
    if csv_path.exists() or parquet_path.exists():
        raise FileExistsError("Compatible exports already exist; choose a new --out directory")
    temp_csv, temp_parquet = csv_path.with_suffix(".csv.tmp"), parquet_path.with_suffix(".parquet.tmp")
    schema = pa.schema([(name, pa.string()) for name in headers])
    count = source.metadata.num_rows
    review_count = sum(int(v) for v in source.read(columns=["review_count"]).column(0).to_pylist())
    sample_file = snapshot / "preview_sample.json"
    if sample_file.exists():
        sample_ids = {p["paper_id"] for p in json.loads(sample_file.read_text(encoding="utf-8"))}
    else:
        first = next(source.iter_batches(batch_size=4, columns=["paper_id"]), None)
        sample_ids = set(first.column(0).to_pylist()) if first is not None else set()
    if finish_existing:
        if not temp_csv.exists() or not temp_parquet.exists():
            raise FileNotFoundError("Both completed .tmp exports are required for --finish-existing")
    else:
        count = 0
        with temp_csv.open("w", newline="", encoding="utf-8") as f, pq.ParquetWriter(temp_parquet, schema, compression="zstd") as writer:
            csv_writer = csv.DictWriter(f, fieldnames=headers)
            csv_writer.writeheader()
            for batch in source.iter_batches(batch_size=500):
                rows = []
                for raw in batch.to_pylist():
                    row = {key: "" for key in headers}
                    row.update(convert_row(raw, mapping, maximum, extras))
                    csv_writer.writerow(row)
                    rows.append(row)
                    count += 1
                writer.write_table(pa.Table.from_pylist(rows, schema=schema))
                if count % 5000 == 0:
                    print(f"Formatted {count}/{source.metadata.num_rows} papers", flush=True)
    print("Checking every retained source column and every CSV/Parquet cell", flush=True)
    verified = validate_files(source_path, temp_parquet, temp_csv, mapping, headers)
    temp_csv.replace(csv_path)
    temp_parquet.replace(parquet_path)
    result = pq.ParquetFile(parquet_path)
    samples = []
    for group in range(result.num_row_groups):
        ids = set(result.read_row_group(group, columns=["paper_id"]).column(0).to_pylist())
        if ids & sample_ids:
            samples += [row for row in result.read_row_group(group).to_pylist() if row["paper_id"] in sample_ids]
    sample_path = output_dir / (basename + "_sample.csv")
    with sample_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(sorted(samples, key=lambda p: p["paper_id"] != "dL4GcShP3x"))
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "source_snapshot": str(snapshot.resolve()),
              "papers": count, "reviews": review_count, "columns": len(headers), "maximum_reviews": maximum,
              "all_source_columns_preserved": True, "csv_parquet_cells_identical": True,
              "verified_rows": verified, "column_mapping": mapping, "additional_public_replies": sum(map(len, extras.values())),
              "files": [str(p.resolve()) for p in (csv_path, parquet_path, sample_path)]}
    (output_dir / (basename + "_validation.json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: val for key, val in report.items() if key != "column_mapping"}, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--finish-existing", action="store_true", help="Validate completed temporary exports without rewriting them")
    args = parser.parse_args()
    export(args.snapshot, args.out, args.finish_existing)
