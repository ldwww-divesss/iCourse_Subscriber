"""Shard the SQLite database into ~10MB encrypted chunks for incremental fetch.

Why shard:
  Once the DB grows past ~50MB the frontend pays a long re-download tax for
  every commit, even if only one course changed. Sharding by course lets the
  frontend hold a content-addressed cache (sha256 → bytes) and re-pull only
  the shards whose hash actually changed, which collapses the typical update
  to <1MB on the wire.

Layout under `output_dir`:
  icourse-index.enc          — encrypted JSON index (small, always re-fetched)
  shards/shard-NNNN.db.gz.enc — encrypted gzipped sqlite, one per shard

Each shard is a self-contained sqlite file holding:
  - the courses rows it owns
  - all lectures referenced by those courses
  - all ppt_pages whose sub_id belongs to those lectures

Reassembly is a straightforward UNION of the per-shard tables.

Trust model: every shard and the index are encrypted with the v2 password
(sha256("ICSv2:" + stuid + ":" + uispsw)). The data branch is public; the
file names and shard count leak, but no row content does.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile

from src.data import crypto_box
from src.data.schema import SCHEMA_SQL as _SCHEMA_SQL

SHARD_TARGET_BYTES = 3 * 1024 * 1024   # encrypted+gzipped target per shard
COMPRESSION_RATIO_GUESS = 4  # gzip ratio for transcript+summary text (Chinese)
INDEX_FILENAME = "icourse-index.enc"
SHARDS_DIR = "shards"
INDEX_VERSION = 2


def _course_uncompressed_size(conn: sqlite3.Connection, course_id: str) -> int:
    """Cheap heuristic for course payload size — sum of text columns."""
    text_size = conn.execute(
        """SELECT COALESCE(SUM(LENGTH(COALESCE(transcript, ''))), 0)
                + COALESCE(SUM(LENGTH(COALESCE(summary, ''))), 0)
           FROM lectures WHERE course_id = ?""",
        (course_id,),
    ).fetchone()[0] or 0

    ppt_size = conn.execute(
        """SELECT COALESCE(SUM(LENGTH(COALESCE(pp.text, ''))), 0)
           FROM ppt_pages pp
           JOIN lectures l ON pp.sub_id = l.sub_id
           WHERE l.course_id = ?""",
        (course_id,),
    ).fetchone()[0] or 0

    return int(text_size) + int(ppt_size)


def _group_courses(
    conn: sqlite3.Connection, target_compressed: int,
) -> list[list[str]]:
    """Pack course_ids into shards, each below ~target_compressed bytes.

    First-fit on courses sorted by course_id (deterministic so shard names
    stay stable when nothing changes). A course exceeding the threshold gets
    its own shard rather than being split across multiple shards.
    """
    target_uncompressed = target_compressed * COMPRESSION_RATIO_GUESS
    courses = [
        r[0] for r in conn.execute(
            "SELECT course_id FROM courses ORDER BY course_id"
        ).fetchall()
    ]
    if not courses:
        return [[]]  # at least one (empty) shard so the index has content

    groups: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for course_id in courses:
        size = _course_uncompressed_size(conn, course_id)
        if current and current_size + size > target_uncompressed:
            groups.append(current)
            current = []
            current_size = 0
        current.append(course_id)
        current_size += size
    if current:
        groups.append(current)
    return groups


def _build_shard_db(source_db: str, course_ids: list[str], output_path: str):
    """Materialize a self-contained sqlite shard for the given courses."""
    if os.path.exists(output_path):
        os.remove(output_path)

    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(output_path)
    try:
        dst.executescript(_SCHEMA_SQL)

        # ``all_courses`` is the school-wide catalog — small (~KB scale)
        # and shared by every shard.  Replicating it into each shard keeps
        # shards self-contained for backup/migration while letting the
        # frontend pick up the catalog without having to merge the index.
        catalog_rows = src.execute("SELECT * FROM all_courses").fetchall()
        if catalog_rows:
            cols = list(catalog_rows[0].keys())
            col_str = ", ".join(cols)
            ph_str = ", ".join("?" * len(cols))
            dst.executemany(
                f"INSERT OR REPLACE INTO all_courses ({col_str}) "
                f"VALUES ({ph_str})",
                [tuple(r[c] for c in cols) for r in catalog_rows],
            )

        if not course_ids:
            dst.commit()
            return

        placeholders = ",".join("?" * len(course_ids))

        course_rows = src.execute(
            f"SELECT * FROM courses WHERE course_id IN ({placeholders})",
            course_ids,
        ).fetchall()
        for row in course_rows:
            dst.execute(
                "INSERT OR REPLACE INTO courses (course_id, title, teacher)"
                " VALUES (?, ?, ?)",
                (row["course_id"], row["title"], row["teacher"]),
            )

        for table in ("lectures", "ppt_pages"):
            if table == "lectures":
                rows = src.execute(
                    f"SELECT * FROM lectures WHERE course_id IN ({placeholders})",
                    course_ids,
                ).fetchall()
            else:
                rows = src.execute(
                    f"""SELECT pp.* FROM ppt_pages pp
                        JOIN lectures l ON pp.sub_id = l.sub_id
                        WHERE l.course_id IN ({placeholders})""",
                    course_ids,
                ).fetchall()
            if not rows:
                continue
            cols = list(rows[0].keys())
            col_str = ", ".join(cols)
            ph_str = ", ".join("?" * len(cols))
            dst.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph_str})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        dst.commit()
    finally:
        dst.close()
        src.close()


def shard_database(
    db_path: str,
    output_dir: str,
    password: str,
    target_size: int = SHARD_TARGET_BYTES,
) -> dict:
    """Split db_path into encrypted shards under output_dir.

    Writes the index to `output_dir/icourse-index.enc` (encrypted JSON) and
    each shard to `output_dir/shards/shard-NNNN.db.gz.enc`. Returns the index
    dict (already serialized to disk).
    """
    shards_dir = os.path.join(output_dir, SHARDS_DIR)
    os.makedirs(shards_dir, exist_ok=True)

    src_conn = sqlite3.connect(db_path)
    try:
        groups = _group_courses(src_conn, target_size)
    finally:
        src_conn.close()

    shard_entries = []
    for i, course_ids in enumerate(groups, start=1):
        name = f"shard-{i:04d}.db.gz.enc"
        shard_path = os.path.join(shards_dir, name)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _build_shard_db(db_path, course_ids, tmp_path)
            with open(tmp_path, "rb") as f:
                raw = f.read()
        finally:
            os.unlink(tmp_path)

        # mtime=0 keeps gzip output deterministic (default writes wall-clock
        # into the header, breaking content-addressed caching downstream).
        gzipped = gzip.compress(raw, compresslevel=9, mtime=0)
        encrypted = crypto_box.encrypt(gzipped, password, deterministic=True)
        sha256 = hashlib.sha256(encrypted).hexdigest()

        with open(shard_path, "wb") as f:
            f.write(encrypted)

        shard_entries.append({
            "name": name,
            "sha256": sha256,
            "size": len(encrypted),
            "course_ids": list(course_ids),
        })

    index = {
        "version": INDEX_VERSION,
        "shards": shard_entries,
    }
    index_bytes = json.dumps(
        index, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    encrypted_index = crypto_box.encrypt(index_bytes, password, deterministic=True)
    with open(os.path.join(output_dir, INDEX_FILENAME), "wb") as f:
        f.write(encrypted_index)

    return index


def load_index(index_path: str, password: str) -> dict:
    """Decrypt and parse an icourse-index.enc file."""
    with open(index_path, "rb") as f:
        encrypted = f.read()
    plaintext = crypto_box.decrypt(encrypted, password)
    if not crypto_box.is_json_obj(plaintext):
        raise ValueError(
            "decrypted index does not look like JSON — wrong password?"
        )
    return json.loads(plaintext)


def _migrate_shard_schema(target: sqlite3.Connection) -> None:
    """Ensure every attached shard has the same columns as ``main``.

    Older shards (created by previous code versions) may lack migration
    columns like ``old_summary``, which causes ``INSERT ... SELECT *``
    to fail with a column-count mismatch.  Adding the missing column
    to the shard before the INSERT makes the ``*`` lists match.
    """
    # Collect column names per table from main
    for table in ("lectures", "ppt_pages", "courses", "all_courses"):
        main_cols = {
            row[1] for row in target.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        }
        shard_cols = {
            row[1] for row in target.execute(
                f"PRAGMA shard.table_info('{table}')"
            ).fetchall()
        }
        # LECTURES_MIGRATION_COLUMNS / PPT_PAGES_MIGRATION_COLUMNS —
        # import here to avoid circular dependency at module level
        from src.data.schema import (
            LECTURES_MIGRATION_COLUMNS,
            PPT_PAGES_MIGRATION_COLUMNS,
        )
        if table == "lectures":
            migrate = LECTURES_MIGRATION_COLUMNS
        elif table == "ppt_pages":
            migrate = PPT_PAGES_MIGRATION_COLUMNS
        else:
            migrate = []
        for col, typedef in migrate:
            if col in main_cols and col not in shard_cols:
                target.execute(
                    f"ALTER TABLE shard.{table} ADD COLUMN {col} {typedef}"
                )


def reassemble_database(
    index: dict, shards_dir: str, output_db: str, password: str,
) -> None:
    """UNION every shard into a fresh sqlite at output_db.

    Used by the CI workflow on first run after the sharded format ships, and
    by the local tests that round-trip through shard → reassemble.
    """
    if os.path.exists(output_db):
        os.remove(output_db)

    target = sqlite3.connect(output_db)
    try:
        target.executescript(_SCHEMA_SQL)
        target.commit()

        for shard in index.get("shards", []):
            shard_path = os.path.join(shards_dir, shard["name"])
            with open(shard_path, "rb") as f:
                encrypted = f.read()
            gzipped = crypto_box.decrypt(encrypted, password)
            if not crypto_box.is_gzip(gzipped):
                raise ValueError(
                    f"decrypted shard {shard['name']!r} is not gzip — "
                    f"wrong password?"
                )
            raw = gzip.decompress(gzipped)

            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            try:
                target.execute("ATTACH DATABASE ? AS shard", (tmp_path,))
                try:
                    # If the shard was produced by an older code version that
                    # lacked a migration column (e.g. old_summary), the
                    # `SELECT *` INSERT would fail with "N columns but M
                    # values".  Add the missing column on the attached shard
                    # first so the `*`-expanded column lists match.
                    _migrate_shard_schema(target)
                    target.execute(
                        "INSERT OR IGNORE INTO main.courses "
                        "SELECT * FROM shard.courses"
                    )
                    target.execute(
                        "INSERT OR IGNORE INTO main.lectures "
                        "SELECT * FROM shard.lectures"
                    )
                    target.execute(
                        "INSERT OR IGNORE INTO main.ppt_pages "
                        "SELECT * FROM shard.ppt_pages"
                    )
                    # all_courses is replicated across every shard with
                    # identical rows; INSERT OR IGNORE means the first
                    # shard fills it and the rest are no-ops on the
                    # (course_id, term) primary key.
                    target.execute(
                        "INSERT OR IGNORE INTO main.all_courses "
                        "SELECT * FROM shard.all_courses"
                    )
                    target.commit()
                finally:
                    target.execute("DETACH DATABASE shard")
            finally:
                os.unlink(tmp_path)
    finally:
        target.close()
