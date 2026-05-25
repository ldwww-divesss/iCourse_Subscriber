/**
 * sql.js wrapper — load lecture data from sharded encrypted shards.
 *
 * In the sharded layout (current), each shard is a self-contained sqlite
 * file holding only the courses + lectures + ppt_pages rows for the courses
 * it owns. The page reassembles them in-memory by copying every shard's
 * rows into a single working SQL.Database. This avoids ATTACH'ing across
 * sql.js DB instances — sql.js doesn't support cross-file ATTACH cleanly,
 * and the row-count is small enough that copying is instantaneous.
 */

window.ICS = window.ICS || {};

const _SQL_CDN = "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.12.0";
let _db = null;
let _SQL = null;

function _schemaSql() {
  // schema.js loads before this file via index.html and registers the SQL
  // on window.ICS.schema so backend (src/schema.py) and frontend share one
  // source of truth.  Throw early if it's missing — silent NULL would
  // produce "no such table" later, which is a worse failure mode.
  var s = window.ICS && window.ICS.schema && window.ICS.schema.SCHEMA_SQL;
  if (!s) throw new Error("ICS.schema.SCHEMA_SQL missing — load js/schema.js first");
  return s;
}

async function _ensureSqlJs() {
  if (_SQL) return _SQL;
  _SQL = await window.initSqlJs({
    locateFile: (file) => `${_SQL_CDN}/${file}`,
  });
  return _SQL;
}

async function _initFromBytes(dbBytes) {
  // Legacy path — accepts a single monolithic sqlite file.
  // Kept so older deployments (pre-shard data branch) still load.
  const SQL = await _ensureSqlJs();
  _db = dbBytes ? new SQL.Database(dbBytes) : new SQL.Database();
  if (!dbBytes) _db.exec(_schemaSql());
}

async function _initEmpty() {
  const SQL = await _ensureSqlJs();
  _db = new SQL.Database();
  _db.exec(_schemaSql());
}

function _copyRows(src, dst, table) {
  const result = src.exec(`SELECT * FROM ${table}`);
  if (!result.length || !result[0].values.length) return;
  const cols = result[0].columns;
  const placeholders = cols.map(() => "?").join(",");
  const stmt = dst.prepare(
    `INSERT OR IGNORE INTO ${table} (${cols.join(",")}) VALUES (${placeholders})`
  );
  for (const row of result[0].values) {
    stmt.bind(row);
    stmt.step();
    stmt.reset();
  }
  stmt.free();
}

async function _attachShard(shardBytes) {
  if (!_db) throw new Error("Database not initialized");
  const SQL = await _ensureSqlJs();
  const shard = new SQL.Database(shardBytes);
  try {
    _copyRows(shard, _db, "courses");
    _copyRows(shard, _db, "lectures");
    _copyRows(shard, _db, "ppt_pages");
    _copyRows(shard, _db, "all_courses");
  } finally {
    shard.close();
  }
}

function _deriveState(row) {
  if (row.error_stage) return "failed";
  if (row.summary && row.processed_at) return "ready";
  if (row.transcript && !row.summary) return "processing";
  return "waiting";
}

function _queryAll(sql, params) {
  if (!_db) return [];
  const stmt = _db.prepare(sql);
  if (params) stmt.bind(params);
  const results = [];
  while (stmt.step()) results.push(stmt.getAsObject());
  stmt.free();
  return results;
}

function _getCourses() {
  return _queryAll(`
    SELECT c.course_id AS course_id, c.title AS title, c.teacher AS teacher,
           COUNT(CASE WHEN l.summary IS NOT NULL THEN 1 END) AS summary_count,
           COUNT(l.sub_id) AS total_count,
           MAX(l.processed_at) AS last_updated
    FROM courses c
    LEFT JOIN lectures l ON c.course_id = l.course_id
    GROUP BY c.course_id
    ORDER BY last_updated DESC NULLS LAST
  `);
}

function _getLectures(courseId) {
  const rows = _queryAll(`
    SELECT sub_id, sub_title, date, summary, processed_at,
           error_stage, error_msg, summary_model, transcript
    FROM lectures WHERE course_id = ? ORDER BY sub_id ASC
  `, [courseId]);
  return rows.map((r) => {
    r.state = _deriveState(r);
    delete r.transcript;
    return r;
  });
}

function _getLecture(subId) {
  const rows = _queryAll(`
    SELECT l.*, c.title AS course_title, c.teacher
    FROM lectures l JOIN courses c ON l.course_id = c.course_id
    WHERE l.sub_id = ?
  `, [subId]);
  if (!rows.length) return null;
  rows[0].state = _deriveState(rows[0]);
  return rows[0];
}

function _getPptPages(subId) {
  // Only return done pages with non-empty text — keeps the PPT viewer
  // free of pending placeholders and dropped pages.
  return _queryAll(`
    SELECT page_num, created_sec, text
    FROM ppt_pages
    WHERE sub_id = ? AND ocr_status = 'done'
      AND text IS NOT NULL AND text != ''
    ORDER BY created_sec ASC
  `, [subId]);
}

function _searchSummaries(query) {
  if (!query?.trim()) return [];
  // Match against summary, transcript, and sub_title.  Transcript is the
  // most useful for "I remember the teacher said X" lookups; summary
  // covers "I read it in the notes"; sub_title covers session names.
  // We mark which field hit so the UI can show the right snippet.
  const q = query;
  return _queryAll(`
    SELECT l.sub_id, l.sub_title, l.summary, l.transcript,
           l.course_id, c.title AS course_title,
           CASE
             WHEN l.summary    LIKE '%' || ? || '%' THEN 'summary'
             WHEN l.sub_title  LIKE '%' || ? || '%' THEN 'sub_title'
             WHEN l.transcript LIKE '%' || ? || '%' THEN 'transcript'
             ELSE 'other'
           END AS hit_field
    FROM lectures l JOIN courses c ON l.course_id = c.course_id
    WHERE l.summary    LIKE '%' || ? || '%'
       OR l.sub_title  LIKE '%' || ? || '%'
       OR l.transcript LIKE '%' || ? || '%'
    ORDER BY l.processed_at DESC LIMIT 50
  `, [q, q, q, q, q, q]);
}

function _getAllCourses(term) {
  // Catalog of every course offered by the school for ``term`` (or all
  // terms if undefined).  Populated by main.py's CRAWL_TERM-driven crawl;
  // empty until that env var has been set at least once on the CI side.
  if (term) {
    return _queryAll(
      "SELECT * FROM all_courses WHERE term = ? ORDER BY title",
      [term],
    );
  }
  return _queryAll(
    "SELECT * FROM all_courses ORDER BY term DESC, title"
  );
}

function _getAllCoursesTerms() {
  return _queryAll(
    "SELECT DISTINCT term FROM all_courses ORDER BY term DESC"
  ).map((r) => r.term);
}

function _getSubscribedCourseIds() {
  // The ``courses`` table holds courses we've actually run.  This is our
  // best signal of "currently subscribed" without reading the
  // COURSE_IDS secret (which GitHub never exposes back).
  return _queryAll("SELECT course_id FROM courses").map((r) => r.course_id);
}

window.ICS.db = {
  initDB: _initFromBytes,
  initEmpty: _initEmpty,
  attachShard: _attachShard,
  getCourses: _getCourses,
  getLectures: _getLectures,
  getLecture: _getLecture,
  getPptPages: _getPptPages,
  searchSummaries: _searchSummaries,
  getAllCourses: _getAllCourses,
  getAllCoursesTerms: _getAllCoursesTerms,
  getSubscribedCourseIds: _getSubscribedCourseIds,
};
