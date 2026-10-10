/**
 * MIRRORS src/schema.py — keep in sync.
 *
 * When you change SCHEMA_SQL or add a migration column on the Python side,
 * update the same content here.  There is no automated sync; the browser
 * builds an in-memory shard set from the same shape the CI runner ships.
 *
 * Differences from src/schema.py: foreign-key clauses and the
 * idx_ppt_pages_sub_status index are dropped because sql.js does not
 * enforce FKs by default and the frontend's row counts are too small for
 * the index to matter.
 */

window.ICS = window.ICS || {};
window.ICS.schema = {
  SCHEMA_SQL: `
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    title TEXT,
    teacher TEXT
);
CREATE TABLE IF NOT EXISTS lectures (
    sub_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_title TEXT, date TEXT,
    transcript TEXT, summary TEXT,
    processed_at TEXT, emailed_at TEXT,
    error_msg TEXT, error_count INTEGER DEFAULT 0,
    error_stage TEXT, summary_model TEXT, last_error_at TEXT
);
CREATE TABLE IF NOT EXISTS ppt_pages (
    sub_id TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    created_sec INTEGER NOT NULL,
    pptimgurl TEXT,
    text TEXT,
    ocr_status TEXT NOT NULL DEFAULT 'pending',
    ocr_at TEXT,
    dhash TEXT,
    PRIMARY KEY (sub_id, page_num)
);
CREATE TABLE IF NOT EXISTS all_courses (
    course_id TEXT NOT NULL,
    term TEXT NOT NULL,
    title TEXT,
    teacher TEXT,
    dept TEXT,
    last_seen_at TEXT,
    PRIMARY KEY (course_id, term)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
`,
};
