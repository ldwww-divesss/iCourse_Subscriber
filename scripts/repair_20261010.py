"""Recover lecture status from the trusted snapshot, keeping new PPT rows.

This repair is specific to run 38048387614. It creates a new output file,
checks that no lecture text changed, and never changes its input files.
"""
import argparse
import sqlite3
from pathlib import Path


def recover(trusted_path: str, affected_path: str, output_path: str,
            sent_count: int = 226, sent_at: str = "2026-10-10T11:38:48.953349") -> dict:
    paths = [Path(value).resolve() for value in (trusted_path, affected_path, output_path)]
    if len(set(paths)) != 3 or paths[2].exists():
        raise ValueError("Use distinct inputs and a new output path")
    trusted = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
    affected = sqlite3.connect(f"file:{paths[1]}?mode=ro", uri=True)
    trusted.row_factory = affected.row_factory = sqlite3.Row
    output = None
    try:
        original = {r["sub_id"]: dict(r) for r in trusted.execute("SELECT * FROM lectures")}
        current = {r["sub_id"]: dict(r) for r in affected.execute("SELECT * FROM lectures")}
        delivered_ids = {sid for sid, row in original.items()
                         if row.get("summary") is not None and row.get("error_stage") is None}
        if len(delivered_ids) != sent_count:
            raise ValueError(f"Sent-set mismatch: expected {sent_count}, found {len(delivered_ids)}")
        for sid, row in original.items():
            if sid not in current:
                raise ValueError("Affected snapshot is missing a trusted lecture")
            for key in ("transcript", "summary"):
                if current[sid].get(key) != row.get(key):
                    raise ValueError("Lecture content changed after the trusted snapshot; manual review required")
        added_ids = set(current) - set(original)
        for sid in added_ids:
            row = current[sid]
            if row.get("summary") or row.get("transcript") or row.get("processed_at"):
                raise ValueError("New processed lecture requires manual review")
        output = sqlite3.connect(paths[2])
        affected.backup(output)
        columns = [row[1] for row in trusted.execute("PRAGMA table_info(lectures)")]
        assignments = ", ".join('"' + col + '" = ?' for col in columns if col != "sub_id")
        fields = [col for col in columns if col != "sub_id"]
        with output:
            for sid, row in original.items():
                if sid in delivered_ids and not row.get("emailed_at"):
                    row["emailed_at"] = sent_at
                output.execute(f"UPDATE lectures SET {assignments} WHERE sub_id = ?",
                               [row.get(col) for col in fields] + [sid])
        if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Recovered database failed integrity check")
        return {"restored_lectures": len(original), "sent_lectures": len(delivered_ids),
                "new_lectures_kept": len(added_ids),
                "ppt_pages_kept": output.execute("SELECT count(*) FROM ppt_pages").fetchone()[0]}
    except Exception:
        if output:
            output.close()
            output = None
        paths[2].unlink(missing_ok=True)
        raise
    finally:
        trusted.close()
        affected.close()
        if output:
            output.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trusted")
    parser.add_argument("affected")
    parser.add_argument("output")
    args = parser.parse_args()
    import json
    print(json.dumps(recover(args.trusted, args.affected, args.output)))


if __name__ == "__main__":
    main()
