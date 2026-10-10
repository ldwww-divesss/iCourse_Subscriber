import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main
from scripts.merge_db import merge
from scripts import provider_env
from src.data.database import Database
from src.data.sharder import shard_database, reassemble_database
from src.runtime import config


class RetryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "local.sqlite")
        self.db = Database(self.path)
        self.db.upsert_course("course", "Course", "Teacher")
        self.db.insert_lecture("old", "course", "Old", "2024-09-01")
        for _ in range(config.MAX_LECTURE_ERRORS):
            self.db.update_error("old", "transcribe", "connection dropped")

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def test_capped_failure_waits_then_recovers(self):
        self.assertEqual(self.db.get_unprocessed_lectures("course"), [])
        timestamp = (datetime.now() - timedelta(
            seconds=config.LECTURE_RETRY_COOLDOWN_SECONDS + 60,
        )).isoformat()
        with self.db.conn:
            self.db.conn.execute("UPDATE lectures SET last_error_at = ?", (timestamp,))
        self.assertEqual([x["sub_id"] for x in self.db.get_unprocessed_lectures("course")], ["old"])
        self.db.update_error("old", "transcribe", "still unavailable")
        self.assertEqual(self.db.get_unprocessed_lectures("course"), [])

    def test_old_database_capped_rows_recover_after_migration(self):
        path = str(Path(self.temp.name) / "legacy.sqlite")
        conn = sqlite3.connect(path)
        # Simulate the previous on-disk schema, without the new timestamp.
        conn.execute("CREATE TABLE lectures (sub_id TEXT PRIMARY KEY, course_id TEXT, "
                     "sub_title TEXT, date TEXT, transcript TEXT, summary TEXT, "
                     "processed_at TEXT, emailed_at TEXT, error_count INTEGER)")
        conn.execute("INSERT INTO lectures (sub_id, course_id, error_count) VALUES ('legacy', 'course', 99)")
        conn.commit()
        conn.close()
        db = Database(path)
        try:
            self.assertEqual(db.get_unprocessed_lectures("course")[0]["sub_id"], "legacy")
        finally:
            db.conn.close()

    def test_recent_recording_precedes_historical_retry(self):
        with self.db.conn:
            self.db.conn.execute("UPDATE lectures SET last_error_at = NULL")
        client = Mock()
        client.get_course_detail.return_value = {
            "title": "Course", "teacher": "Teacher", "lectures": [
                {"sub_id": "old", "date": "2024-09-01", "has_playback": True},
                {"sub_id": "new", "date": "2026-10-09", "has_playback": True},
            ],
        }
        with patch.object(config, "COURSE_IDS", ["course"]), patch.object(main, "_check_session"):
            queued = main._enumerate_lectures(client, self.db, Mock())
        self.assertEqual([x[2]["sub_id"] for x in queued], ["new", "old"])

    def test_merge_preserves_cooldown_and_success_clears_it(self):
        remote_path = str(Path(self.temp.name) / "remote.sqlite")
        remote = Database(remote_path)
        remote.conn.close()
        merge(self.path, remote_path)
        remote = Database(remote_path)
        try:
            self.assertEqual(remote.get_lecture("old")["last_error_at"],
                             self.db.get_lecture("old")["last_error_at"])
            self.assertEqual(remote.get_unprocessed_lectures("course"), [])
            self.db.mark_processed("old")
            self.db.clear_error("old")
            merge(self.path, remote_path)
            self.assertIsNone(remote.get_lecture("old")["last_error_at"])
            self.assertEqual(remote.get_unprocessed_lectures("course"), [])
        finally:
            remote.conn.close()

    def test_encrypted_shard_roundtrip_preserves_retry_timestamp(self):
        output = str(Path(self.temp.name) / "shards")
        restored_path = str(Path(self.temp.name) / "restored.sqlite")
        timestamp = self.db.get_lecture("old")["last_error_at"]
        index = shard_database(self.path, output, "test-password")
        reassemble_database(index, str(Path(output) / "shards"),
                            restored_path, "test-password")
        restored = Database(restored_path)
        try:
            self.assertEqual(restored.get_lecture("old")["last_error_at"], timestamp)
            self.assertEqual(restored.get_unprocessed_lectures("course"), [])
        finally:
            restored.conn.close()


class CatalogRefreshTests(unittest.TestCase):
    def check(self, day):
        db, client = Mock(), Mock()
        db.list_catalog_terms.return_value = {"known"}
        client.discover_terms.return_value = [
            {"code": "35", "name": "new", "count": 1},
            {"code": "34", "name": "known", "count": 1},
        ]
        client.list_semester_courses.return_value = [{"course_id": "course"}]
        db.upsert_all_courses_for_term.return_value = (0, 1)
        with patch.object(main, "_check_session"), patch.object(main, "datetime") as clock:
            clock.now.return_value.day = day
            main._crawl_semester_catalog(client, db, Mock())
        return client

    def test_new_terms_fetched_each_run(self):
        self.check(10).list_semester_courses.assert_called_once_with("35")

    def test_known_terms_refreshed_on_both_dates(self):
        for day in (9, 25):
            with self.subTest(day=day):
                self.assertEqual(self.check(day).list_semester_courses.call_count, 2)


class ProviderLauncherTests(unittest.TestCase):
    def test_only_provider_keys_are_resolved(self):
        env = provider_env.provider_environment({"dashscope_api_key": "test-key", "UISPSW": "private"})
        self.assertEqual(env, {"DASHSCOPE_API_KEY": "test-key"})

    def test_literal_secret_and_context_removal_in_real_child(self):
        secret = "literal ' $(touch should-not-exist) `command`\nvalue"
        env = dict(os.environ, SECRETS_CONTEXT=json.dumps({
            "DASHSCOPE_API_KEY": secret, "UNRELATED_SECRET": "private",
        }))
        code = ("import os; assert 'SECRETS_CONTEXT' not in os.environ; "
                "assert 'UNRELATED_SECRET' not in os.environ; "
                f"assert os.environ['DASHSCOPE_API_KEY'] == {secret!r}")
        result = subprocess.run([sys.executable, "scripts/provider_env.py", "--",
                                 sys.executable, "-c", code], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"")

    def test_invalid_context_fails_before_launching_child(self):
        with patch.dict(os.environ, {"SECRETS_CONTEXT": "invalid"}), \
                patch.object(sys, "argv", ["provider_env.py", "--", "python", "main.py"]), \
                patch.object(provider_env.subprocess, "call") as child:
            self.assertEqual(provider_env.main(), 1)
            child.assert_not_called()

    def test_invalid_variable_name_is_rejected(self):
        with patch.object(provider_env, "MODEL_PROVIDERS", [{"api_key_env": "KEY;command"}]):
            with self.assertRaises(ValueError):
                provider_env.provider_environment({})
