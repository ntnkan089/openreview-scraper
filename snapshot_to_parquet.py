"""Create bounded-memory, lossless Parquet companions for snapshot CSVs."""
import argparse
import csv
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def convert(folder):
    csv.field_size_limit(2 ** 31 - 1)
    for name in ("papers", "reviews"):
        source = folder / (name + ".csv")
        destination = folder / (name + ".parquet")
        if destination.exists():
            print(f"Already exists: {destination}", flush=True)
            continue
        temp = destination.with_suffix(".parquet.tmp")
        count = 0
        with source.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            schema = pa.schema([(key, pa.string()) for key in reader.fieldnames])
            with pq.ParquetWriter(temp, schema, compression="zstd") as writer:
                batch = []
                for row in reader:
                    batch.append(row)
                    count += 1
                    if len(batch) >= 500:
                        writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                        batch.clear()
                if batch:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
        if pq.read_metadata(temp).num_rows != count:
            raise RuntimeError("Parquet row count differs from CSV")
        temp.replace(destination)
        print(f"{destination.name}: {count} rows, {destination.stat().st_size} bytes", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    convert(parser.parse_args().snapshot)
