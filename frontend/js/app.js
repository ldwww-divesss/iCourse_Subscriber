/**
 * Alpine.js app — all state, routing, and view logic for the iCourse frontend.
 * References ICS.crypto, ICS.github, ICS.db, ICS.render globals.
 */

/* ── Gzip helpers (Compression Streams API) ── */
async function _gunzip(compressedBytes) {
  var ds = new DecompressionStream("gzip");
  var writer = ds.writable.getWriter();
  writer.write(compressedBytes);
  writer.close();
  var chunks = [];
  var reader = ds.readable.getReader();
  while (true) {
    var r = await reader.read();
    if (r.done) break;
    chunks.push(r.value);
  }
  var total = chunks.reduce(function(s, c) { return s + c.length; }, 0);
  var result = new Uint8Array(total);
  var offset = 0;
  for (var i = 0; i < chunks.length; i++) {
    result.set(chunks[i], offset);
    offset += chunks[i].length;
  }
  return result;
}

/* ── IndexedDB cache for decrypted shards (keyed by git blob sha) ────
   Shard contents are content-addressed: a shard's git blob sha changes
   only when its bytes change, so we can keep decrypted bytes around and
   skip the network + decrypt + decompress chain on subsequent loads.
*/
var _idbName = "ics_cache_v2";

function _idbOpen() {
  return new Promise(function(resolve, reject) {
    var req = indexedDB.open(_idbName, 1);
    req.onupgradeneeded = function() { req.result.createObjectStore("blobs"); };
    req.onsuccess = function() { resolve(req.result); };
    req.onerror = function() { reject(req.error); };
  });
}

async function _idbGet(key) {
  var db = await _idbOpen();
  return new Promise(function(resolve) {
    var tx = db.transaction("blobs", "readonly");
    var req = tx.objectStore("blobs").get(key);
    req.onsuccess = function() { resolve(req.result || null); };
    req.onerror = function() { resolve(null); };
  });
}

async function _idbPut(key, value) {
  var db = await _idbOpen();
  return new Promise(function(resolve) {
    var tx = db.transaction("blobs", "readwrite");
    tx.objectStore("blobs").put(value, key);
    tx.oncomplete = function() { resolve(); };
    tx.onerror = function() { resolve(); };
  });
}

/* ── Credential helpers (localStorage) ── */
const _LS = "ics_";
const _loadCreds = () => { try { return JSON.parse(localStorage.getItem(_LS + "creds")); } catch { return null; } };
const _saveCreds = (c) => localStorage.setItem(_LS + "creds", JSON.stringify(c));
const _loadSettings = () => { try { return JSON.parse(localStorage.getItem(_LS + "settings")) || {}; } catch { return {}; } };
const _saveSettings = (s) => localStorage.setItem(_LS + "settings", JSON.stringify(s));
/* Starred-course IDs are per-browser (localStorage). The school side
   doesn't need to know; the user just wants their favorites pinned to
   the top of their own view. */
const _loadStarred = () => {
  try { return new Set(JSON.parse(localStorage.getItem(_LS + "starred")) || []); }
  catch { return new Set(); }
};
const _saveStarred = (set) => localStorage.setItem(
  _LS + "starred", JSON.stringify(Array.from(set))
);

function _relativeTime(iso) {
  if (!iso) return "";
  const d = Date.now() - new Date(iso).getTime();
  const m = Math.floor(d / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  const days = Math.floor(h / 24);
  if (days < 30) return days + "d ago";
  return new Date(iso).toLocaleDateString();
}

function _highlightSnippet(text, query, radius) {
  radius = radius || 60;
  if (!text || !query) return "";
  const plain = ICS.render.plainSnippet(text, 99999);
  const idx = plain.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return plain.slice(0, 120) + "...";
  const s = Math.max(0, idx - radius);
  const e = Math.min(plain.length, idx + query.length + radius);
  let snip = (s > 0 ? "..." : "") + plain.slice(s, e) + (e < plain.length ? "..." : "");
  const re = new RegExp("(" + query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
  return snip.replace(re, "<mark>$1</mark>");
}

function _formatTimestamp(seconds) {
  var sec = Math.max(0, Math.floor(seconds || 0));
  var h = Math.floor(sec / 3600);
  var m = Math.floor((sec % 3600) / 60);
  var s = sec % 60;
  function pad(n) { return String(n).padStart(2, "0"); }
  if (h > 0) return pad(h) + ":" + pad(m) + ":" + pad(s);
  return pad(m) + ":" + pad(s);
}

/* Three-state detail view: summary → transcript → ppt → summary.
   The button label always shows the *next* state so the user can read it
   as an action ("切换到转录"). */
const _DETAIL_VIEW_CYCLE = ["summary", "transcript", "ppt"];
const _DETAIL_VIEW_LABEL = {
  summary: "摘要",
  transcript: "转录",
  ppt: "PPT 识别",
};

/* ── Sharded loading helpers ── */
async function _loadShard(owner, repo, entry, password, token) {
  // Hit cache first; on miss download → decrypt → gunzip and store the
  // decompressed sqlite bytes (~4× compression ratio, well under IndexedDB
  // quota for typical class sizes).
  var cacheKey = "shard:" + entry.sha;
  var cached = await _idbGet(cacheKey);
  if (cached) return cached;

  var encBytes = await ICS.github.fetchBlobBytes(owner, repo, entry.sha, token);
  var gzipped = await ICS.crypto.decrypt(
    encBytes, password, ICS.crypto.NEW_ITERATIONS,
  );
  if (!ICS.crypto.isGzip(gzipped)) {
    throw new Error(
      "Shard '" + entry.name + "' decrypted to non-gzip bytes — wrong key?"
    );
  }
  var dbBytes = await _gunzip(gzipped);
  await _idbPut(cacheKey, dbBytes);
  return dbBytes;
}

async function _loadFromShardManifest(manifest, owner, repo, password, token, progress) {
  // 1) Fetch + decrypt the index (small, never cached)
  var indexEnc = await ICS.github.fetchBlobBytes(
    owner, repo, manifest.index.sha, token,
  );
  var indexBytes = await ICS.crypto.decrypt(
    indexEnc, password, ICS.crypto.NEW_ITERATIONS,
  );
  if (!ICS.crypto.isJsonObj(indexBytes)) {
    throw new Error("Shard index decrypted to non-JSON bytes — wrong key?");
  }
  var index = JSON.parse(new TextDecoder().decode(indexBytes));

  // 2) Pull every shard (cache hits short-circuit, so only changed shards
  //    actually download) and merge them into one in-memory DB.
  await ICS.db.initEmpty();
  var total = (index.shards || []).length;
  for (var i = 0; i < total; i++) {
    var shardMeta = index.shards[i];
    var entry = manifest.shards.find(function (s) { return s.name === shardMeta.name; });
    if (!entry) {
      console.warn("Shard listed in index but missing from tree:", shardMeta.name);
      continue;
    }
    if (progress) progress(i + 1, total, shardMeta.name);
    var shardBytes = await _loadShard(owner, repo, entry, password, token);
    await ICS.db.attachShard(shardBytes);
  }
}

async function _loadFromLegacyBlob(manifest, owner, repo, secrets, token) {
  // Single-file fallback for users still on the pre-shard data branch.
  var encBytes = await ICS.github.fetchBlobBytes(
    owner, repo, manifest.legacy.sha, token,
  );
  var validator = manifest.legacy.compressed
    ? ICS.crypto.isGzip
    : ICS.crypto.isSqlite;
  var fallback = await ICS.crypto.decryptWithFallback(
    encBytes, secrets, validator,
  );
  var bytes = fallback.data;
  if (manifest.legacy.compressed) {
    bytes = await _gunzip(bytes);
  }
  await ICS.db.initDB(bytes);
}

/* ── Alpine app ── */
document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    view: "loading", error: null, loadingMsg: "",
    toast: null, toastType: "success",
    courses: [], lectures: [],
    currentCourse: null, currentLecture: null,
    currentPptPages: [],
    detailView: "summary",
    searchQuery: "", searchResults: [],
    commitSha: null,
    setup: { token: "", stuid: "", uispsw: "" },
    setupError: "", setupTesting: false,
    settingsForm: {}, showSecrets: {},
    exportDialogOpen: false, exportSelection: {}, exportingPdf: false,
    iterations: 100000, repoOwner: "", repoName: "", dataBranch: "data",
    _history: [],
    /* Subscriptions editor state. ``subsSelectedIds`` is the working list
       the user is editing; ``subsCurrentIds`` is the read-only snapshot
       of what's actually deployed (from the courses table) so we can
       offer a "reset" button. */
    allCourses: [], allCoursesTerms: [],
    subsTerm: "", subsSearch: "",
    subsSelectedIds: [], subsCurrentIds: [], subsFiltered: [],
    subsSaving: false, subsError: "",
    triggeringCheck: false,
    /* Per-browser pinned-courses set, lazily synced to localStorage. */
    starred: _loadStarred(),

    async init() {
      const detected = ICS.github.detectRepo();
      const s = _loadSettings();
      this.repoOwner = s.owner || (detected?.owner ?? "");
      this.repoName = s.repo || (detected?.repo ?? "");
      this.dataBranch = s.branch || "data";
      this.iterations = s.iterations || 100000;
      const creds = _loadCreds();
      if (!creds) { this.view = "setup"; return; }
      await this._loadDB(creds);
    },

    async _loadDB(creds) {
      this.view = "loading"; this.error = null;
      try {
        this.loadingMsg = "Connecting to GitHub API...";
        var manifest = await ICS.github.fetchShardManifest(
          this.repoOwner, this.repoName, this.dataBranch, creds.token,
        );
        this.commitSha = manifest.commitSha;

        if (manifest.format === "sharded") {
          this.loadingMsg = "Deriving decryption key...";
          var pw = await ICS.crypto.buildPasswordV2(creds);

          this.loadingMsg = "Downloading + decrypting shard index...";
          var self = this;
          await _loadFromShardManifest(
            manifest, this.repoOwner, this.repoName, pw, creds.token,
            function (i, n, name) {
              self.loadingMsg = "Shard " + i + "/" + n
                + " — downloading + decrypting (" + name + ")...";
            },
          );
        } else {
          this.loadingMsg = "Downloading + decrypting legacy database...";
          await _loadFromLegacyBlob(
            manifest, this.repoOwner, this.repoName, creds, creds.token,
          );
        }

        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
        this.view = "courses";
      } catch (e) {
        this.error = e.message;
        this.view = "error";
      }
    },

    navigate(view, params) {
      params = params || {};
      this._history.push({ view: this.view, courseId: this.currentCourse?.course_id, lectureId: this.currentLecture?.sub_id });
      this._go(view, params);
    },
    _go(view, params) {
      params = params || {};
      this.error = null;
      if (view === "courses") {
        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
      }
      else if (view === "lectures" && params.courseId) {
        this.currentCourse = this.courses.find(x => x.course_id === params.courseId) || { course_id: params.courseId, title: "...", teacher: "" };
        this.lectures = ICS.db.getLectures(params.courseId);
      }
      else if (view === "detail" && params.subId) {
        this.currentLecture = ICS.db.getLecture(params.subId);
        this.currentPptPages = this.currentLecture
          ? ICS.db.getPptPages(this.currentLecture.sub_id)
          : [];
        this.detailView = "summary";
      }
      this.view = view;
      if (view !== "lectures") this.exportDialogOpen = false;
    },
    _sortCoursesByStar(list) {
      // Stable two-key sort: starred first (descending = pinned), then
      // by the existing last_updated DESC the SQL already produced.
      var starred = this.starred;
      return list.slice().sort(function (a, b) {
        var sa = starred.has(String(a.course_id)) ? 0 : 1;
        var sb = starred.has(String(b.course_id)) ? 0 : 1;
        if (sa !== sb) return sa - sb;
        return 0;  // preserve SQL order within each group
      });
    },
    goBack() {
      const p = this._history.pop();
      if (p) this._go(p.view, { courseId: p.courseId, subId: p.lectureId });
      else this._go("courses");
    },

    openCourse(id) { this.navigate("lectures", { courseId: id }); },
    openLecture(id) { this.navigate("detail", { subId: id }); },

    /* Prev/next within the current course's lecture list.  Lectures are
       ordered ascending by sub_id (matches the lectures view), so "prev"
       is the lecture at index-1 and "next" is at index+1. */
    _currentLectureIndex() {
      if (!this.currentLecture || !this.lectures) return -1;
      return this.lectures.findIndex(
        (l) => String(l.sub_id) === String(this.currentLecture.sub_id)
      );
    },
    prevLecture() {
      var i = this._currentLectureIndex();
      return i > 0 ? this.lectures[i - 1] : null;
    },
    nextLecture() {
      var i = this._currentLectureIndex();
      return (i >= 0 && i + 1 < this.lectures.length)
        ? this.lectures[i + 1] : null;
    },
    gotoPrevLecture() {
      var lec = this.prevLecture();
      if (lec) { this._go("detail", { subId: lec.sub_id }); this._scrollToTop(); }
    },
    gotoNextLecture() {
      var lec = this.nextLecture();
      if (lec) { this._go("detail", { subId: lec.sub_id }); this._scrollToTop(); }
    },
    _scrollToTop() {
      var self = this;
      this.$nextTick(function () {
        var el = document.querySelector("main");
        if (el) el.scrollTop = 0;
      });
    },

    /* Star/pin a course.  Per-browser localStorage state; no roundtrip
       to GitHub.  Re-sorts the courses list immediately so the user
       sees the pin take effect without navigating away. */
    isStarred(courseId) {
      return this.starred.has(String(courseId));
    },
    toggleStar(courseId) {
      var cid = String(courseId);
      if (this.starred.has(cid)) this.starred.delete(cid);
      else this.starred.add(cid);
      _saveStarred(this.starred);
      // Reactive refresh — re-sort in place.
      this.courses = this._sortCoursesByStar(this.courses);
    },

    /* Three-state detail viewer.  The button shown to the user always
       advertises the *next* state so the label reads as an action. */
    cycleDetailView() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      if (idx === -1) idx = 0;
      this.detailView = _DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length];
    },
    nextDetailViewLabel() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      if (idx === -1) idx = 0;
      var next = _DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length];
      return "切换到" + _DETAIL_VIEW_LABEL[next];
    },
    formatPptTimestamp(sec) { return _formatTimestamp(sec); },

    getExportableLectures() {
      return (this.lectures || []).filter((lec) => lec.summary && lec.summary.trim());
    },
    openExportDialog() {
      const list = this.getExportableLectures();
      if (!list.length) { this._toast("No summarized lectures to export", "error"); return; }
      this.exportSelection = {};
      list.forEach((lec) => { this.exportSelection[lec.sub_id] = true; });
      this.exportDialogOpen = true;
    },
    closeExportDialog() {
      if (this.exportingPdf) return;
      this.exportDialogOpen = false;
    },
    isLectureSelected(subId) { return !!this.exportSelection[subId]; },
    toggleLectureSelection(subId, checked) { this.exportSelection[subId] = !!checked; },
    setExportAll(checked) {
      this.getExportableLectures().forEach((lec) => { this.exportSelection[lec.sub_id] = !!checked; });
    },
    isExportAllSelected() {
      const list = this.getExportableLectures();
      return list.length > 0 && list.every((lec) => this.exportSelection[lec.sub_id]);
    },
    selectedExportCount() {
      return this.getExportableLectures().filter((lec) => this.exportSelection[lec.sub_id]).length;
    },
    async exportSelectedToPdf() {
      // Triggers .github/workflows/export.yml via workflow_dispatch.  The
      // workflow runs scripts/export_course.py (WeasyPrint) and emails the
      // PDF to RECEIVER_EMAIL — same output and same code path as a manual
      // run from the Actions UI.  We dropped the in-browser html2pdf.js
      // approach because the screenshot-based pipeline produced blank PDFs
      // unreliably; routing through Actions reuses the working tech stack.
      if (this.exportingPdf) return;
      const selected = this.getExportableLectures().filter(
        (lec) => this.exportSelection[lec.sub_id]
      );
      if (!selected.length) {
        this._toast("Please select at least one lecture", "error");
        return;
      }
      const creds = _loadCreds();
      if (!creds?.token) {
        this._toast("Not authenticated", "error");
        return;
      }
      this.exportingPdf = true;
      try {
        const subIds = selected.map((lec) => String(lec.sub_id)).join(",");
        // Workflow files live on the default branch (main).  Surfaced as a
        // hardcoded "main" for now; expose as a setting if users rename it.
        await ICS.github.triggerExportWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          this.currentCourse.course_id, true, subIds
        );
        this.exportDialogOpen = false;
        this._toast(
          "已触发后台导出，PDF 将在 1-3 分钟内发送到 RECEIVER_EMAIL",
          "success"
        );
      } catch (e) {
        this._toast(e?.message || "Export failed", "error");
      } finally {
        this.exportingPdf = false;
      }
    },

    _searchTimeout: null,
    doSearch() {
      clearTimeout(this._searchTimeout);
      this._searchTimeout = setTimeout(() => {
        this.searchResults = this.searchQuery.trim() ? ICS.db.searchSummaries(this.searchQuery) : [];
      }, 300);
    },

    async refresh() {
      const c = _loadCreds();
      if (c) { await this._loadDB(c); this._toast("Refreshed", "success"); }
    },

    async testAndSave() {
      this.setupTesting = true; this.setupError = "";
      try {
        var manifest = await ICS.github.fetchShardManifest(
          this.repoOwner, this.repoName, this.dataBranch, this.setup.token,
        );
        if (manifest.format === "sharded") {
          // Probe the index decryption to validate creds before we save.
          var pw = await ICS.crypto.buildPasswordV2(this.setup);
          var indexEnc = await ICS.github.fetchBlobBytes(
            this.repoOwner, this.repoName, manifest.index.sha, this.setup.token,
          );
          var indexPt = await ICS.crypto.decrypt(
            indexEnc, pw, ICS.crypto.NEW_ITERATIONS,
          );
          if (!ICS.crypto.isJsonObj(indexPt)) {
            throw new Error("凭据验证失败：索引解密结果不像 JSON。");
          }
        } else {
          var encBytes = await ICS.github.fetchBlobBytes(
            this.repoOwner, this.repoName, manifest.legacy.sha, this.setup.token,
          );
          var legacyValidator = manifest.legacy.compressed
            ? ICS.crypto.isGzip
            : ICS.crypto.isSqlite;
          await ICS.crypto.decryptWithFallback(
            encBytes, this.setup, legacyValidator,
          );
        }
        _saveCreds({ ...this.setup });
        _saveSettings({ owner: this.repoOwner, repo: this.repoName, branch: this.dataBranch, iterations: this.iterations });
        this.commitSha = manifest.commitSha;
        await this._loadDB({ ...this.setup });
      } catch (e) { this.setupError = e.message; }
      finally { this.setupTesting = false; }
    },

    openSettings() {
      this.settingsForm = { ...(_loadCreds() || {}) };
      this.showSecrets = {};
      this.navigate("settings");
    },
    async saveSettingsAndReload() {
      _saveCreds({ ...this.settingsForm });
      _saveSettings({ owner: this.repoOwner, repo: this.repoName, branch: this.dataBranch, iterations: this.iterations });
      this._toast("Saved. Reloading...", "success");
      const c = _loadCreds();
      if (c) await this._loadDB(c);
    },
    clearAllData() {
      if (!confirm("Clear all saved credentials?")) return;
      localStorage.removeItem(_LS + "creds");
      localStorage.removeItem(_LS + "settings");
      indexedDB.deleteDatabase(_idbName);
      this.view = "setup";
      this.setup = { token: "", stuid: "", uispsw: "" };
    },

    // ── Subscriptions editor ────────────────────────────────────────
    openSubscriptions() {
      // Pull the catalog + currently-subscribed list from the DB (sourced
      // from the courses table; that's our best signal of "what's running"
      // since GitHub never lets us read secret values back).  When the
      // user just saved a new list this run, the localStorage cache wins
      // — it reflects what was actually pushed to the secret, not the
      // stale ``courses`` snapshot.
      this.allCourses = ICS.db.getAllCourses();
      this.allCoursesTerms = ICS.db.getAllCoursesTerms();
      this.subsTerm = this.allCoursesTerms[0] || "";
      this.subsSearch = "";
      let current = ICS.db.getSubscribedCourseIds().map(String);
      try {
        const cached = JSON.parse(
          localStorage.getItem(_LS + "lastSubscribed") || "null"
        );
        if (Array.isArray(cached) && cached.length) {
          // Union — if a previously-subscribed course is also in the DB
          // (workflow already ran for it) we keep it; if only in cache,
          // also keep it so the user doesn't lose their pending save.
          const seen = new Set(current.map(String));
          for (const cid of cached) {
            if (!seen.has(String(cid))) current.push(String(cid));
          }
        }
      } catch {}
      this.subsCurrentIds = current;
      this.subsSelectedIds = this.subsCurrentIds.slice();
      this.subsError = "";
      this.rebuildSubsFiltered();
      this.navigate("subscriptions");
    },
    get allCoursesForTerm() {
      if (!this.subsTerm) return this.allCourses;
      return this.allCourses.filter((c) => c.term === this.subsTerm);
    },
    rebuildSubsFiltered() {
      const q = (this.subsSearch || "").trim().toLowerCase();
      const list = this.allCoursesForTerm;
      // Subscribed rows always float to the top so the user sees current
      // selections without scrolling, regardless of search filter.
      const selected = new Set(this.subsSelectedIds.map(String));
      const ranked = list.map((c) => ({
        c,
        score: selected.has(String(c.course_id)) ? 0 : 1,
      }));
      const filtered = ranked.filter(({ c }) => {
        if (!q) return true;
        return [c.title, c.teacher, c.dept, c.course_id]
          .filter(Boolean)
          .some((s) => String(s).toLowerCase().includes(q));
      });
      filtered.sort((a, b) => a.score - b.score
        || String(a.c.title || "").localeCompare(String(b.c.title || "")));
      this.subsFiltered = filtered.map(({ c }) => c);
    },
    toggleSubscription(courseId, checked) {
      const cid = String(courseId);
      const cur = new Set(this.subsSelectedIds.map(String));
      if (checked) cur.add(cid); else cur.delete(cid);
      this.subsSelectedIds = Array.from(cur);
      // No re-sort on toggle — keeps the just-clicked row stable so the
      // user can rapidly toggle multiple rows without the UI jumping.
    },
    resetSubscriptionsToCurrent() {
      this.subsSelectedIds = this.subsCurrentIds.slice();
      this.subsError = "";
      this.rebuildSubsFiltered();
    },
    async saveSubscriptions() {
      if (this.subsSaving) return;
      const creds = _loadCreds();
      if (!creds?.token) {
        this.subsError = "未登录或 PAT 缺失。";
        return;
      }
      if (!this.repoOwner || !this.repoName) {
        this.subsError = "Repo owner/name 未设置，请到 Settings 配置。";
        return;
      }
      this.subsSaving = true;
      this.subsError = "";
      try {
        const written = await ICS.github.setCourseIdsSecret(
          this.repoOwner, this.repoName, creds.token, this.subsSelectedIds,
        );
        this._toast(
          `已保存 ${written.split(",").filter(Boolean).length} 门课到 COURSE_IDS secret`,
          "success",
        );
        // Treat the saved state as the new "current" so subsequent
        // re-opens of the editor compare against it correctly.  We don't
        // re-fetch courses table — that updates only after the next
        // workflow run creates the lectures.
        this.subsCurrentIds = this.subsSelectedIds.slice();
        // Remember the saved set across reloads so that if user re-opens
        // the editor before a workflow run, they see what they just saved
        // rather than the stale `courses` snapshot.
        try {
          localStorage.setItem(
            _LS + "lastSubscribed",
            JSON.stringify(this.subsCurrentIds),
          );
        } catch {}
      } catch (e) {
        this.subsError = e?.message || "保存失败";
      } finally {
        this.subsSaving = false;
      }
    },

    async triggerCheckRun() {
      // Fires the daily check workflow on demand so the user can see their
      // newly-saved subscription list applied without waiting for the
      // scheduled cron.
      if (this.triggeringCheck) return;
      const creds = _loadCreds();
      if (!creds?.token) {
        this.subsError = "未登录或 PAT 缺失。";
        return;
      }
      this.triggeringCheck = true;
      this.subsError = "";
      try {
        await ICS.github.triggerCheckWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
        );
        this._toast("已触发 workflow，请到 Actions 标签查看进度", "success");
      } catch (e) {
        this.subsError = e?.message || "触发失败";
      } finally {
        this.triggeringCheck = false;
      }
    },

    _toast(msg, type) {
      this.toast = msg; this.toastType = type || "success";
      setTimeout(() => { this.toast = null; }, 3000);
    },

    // Template helpers
    renderMd(s) { return ICS.render.renderMarkdown(s); },
    activateKaTeX(el) { ICS.render.activateKaTeX(el); },
    snippet(s, n) { return ICS.render.plainSnippet(s, n); },
    highlight(text, q) { return _highlightSnippet(text, q); },
    relTime(s) { return _relativeTime(s); },
  }));
});
