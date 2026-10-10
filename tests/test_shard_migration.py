import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.repair_20261010 import recover
from src.data import sharder
from src.data.database import Database
from src.data.schema import SCHEMA_SQL


class ShardMigrationTests(unittest.TestCase):
    def test_encrypted_old_schema_preserves_every_lecture_field(self):
        old_schema = SCHEMA_SQL.replace("    transcript_partial INTEGER DEFAULT 0,\n    audio_received_seconds REAL, audio_expected_seconds REAL,\n", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = sqlite3.connect(root / "old.db")
            old.executescript(old_schema)
            old.execute("INSERT INTO courses VALUES (?,?,?)", ("course", "Course", "Teacher"))
            old.execute("INSERT INTO lectures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("lecture", "course", "Title", "2026-10-08", "Speech", "Notes",
                         None, "2026-10-01T10:00:00", "connection dropped", 3,
                         "transcribe", "model", "2026-10-10T09:52:00"))
            expected = dict(zip([r[1] for r in old.execute("PRAGMA table_info(lectures)")],
                                old.execute("SELECT * FROM lectures").fetchone()))
            old.commit()
            old.close()
            with patch.object(sharder, "_SCHEMA_SQL", old_schema):
                index = sharder.shard_database(str(root / "old.db"), str(root / "shards"), "password")
            sharder.reassemble_database(index, str(root / "shards" / "shards"), str(root / "restored.db"), "password")
            restored = Database(str(root / "restored.db"))
            try:
                row = restored.get_lecture("lecture")
                for key, value in expected.items():
                    self.assertEqual(row[key], value, key)
                self.assertFalse(row["transcript_partial"])
                self.assertIsNone(row["audio_expected_seconds"])
            finally:
                restored.conn.close()


class StatusRecoveryTests(unittest.TestCase):
    def test_processing_rejects_shifted_state_before_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "affected.db")
            db = Database(path)
            db.insert_lecture("lecture", "course", "Title", "2026-10-08")
            db.conn.execute("UPDATE lectures SET processed_at='3', error_count='2026-10-10T09:52:00'")
            db.conn.commit()
            db.conn.close()
            with self.assertRaisesRegex(RuntimeError, "Invalid lecture status"):
                Database(path)

    def test_restore_metadata_keep_new_ppt_and_actual_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = Database(str(root / "trusted.db"))
            trusted.upsert_course("course", "Course", "Teacher")
            trusted.insert_lecture("lecture", "course", "Title", "2026-10-08")
            trusted.update_transcript("lecture", "Speech")
            trusted.update_summary("lecture", "Notes", "model")
            trusted.mark_processed("lecture")
            copy = sqlite3.connect(root / "affected.db")
            trusted.conn.backup(copy)
            copy.close()
            affected = Database(str(root / "affected.db"))
            affected.conn.execute("UPDATE lectures SET processed_at='0', error_count=NULL, error_stage='0', emailed_at='2026-10-10T11:38:48'")
            affected.conn.execute("INSERT INTO ppt_pages (sub_id,page_num,created_sec,text,ocr_status) VALUES ('lecture',1,10,'New slide','done')")
            affected.conn.commit()
            trusted.conn.close()
            affected.conn.close()
            result = recover(str(root / "trusted.db"), str(root / "affected.db"), str(root / "output.db"), sent_count=1)
            output = Database(str(root / "output.db"))
            try:
                row = output.get_lecture("lecture")
                self.assertTrue(row["processed_at"].startswith("2026-"))
                self.assertIsNone(row["error_stage"])
                self.assertEqual(row["error_count"], 0)
                self.assertEqual(row["emailed_at"], "2026-10-10T11:38:48.953349")
                self.assertEqual(result["ppt_pages_kept"], 1)
            finally:
                output.conn.close()

    def test_repair_fails_before_writing_on_unexpected_delivery_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("trusted", "affected"):
                db = Database(str(root / (name + ".db")))
                db.conn.close()
            with self.assertRaisesRegex(ValueError, "Sent-set mismatch"):
                recover(str(root / "trusted.db"), str(root / "affected.db"), str(root / "output.db"))
            self.assertFalse((root / "output.db").exists())
