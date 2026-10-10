import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import main

from scripts.merge_db import merge
from src.ai.transcriber import Transcriber, IncompleteAudioError
from src.api.icourse import ICourseClient
from src.data.database import Database
from src.data.sharder import shard_database, reassemble_database
from src.pipeline.lecture_runner import LectureRunner


class PartialAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = self.database("local")

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def database(self, name):
        db = Database(str(Path(self.temp.name) / (name + ".db")))
        db.upsert_course("course", "Course", "Teacher")
        db.insert_lecture("lecture", "course", "Lecture", "2026-10-08")
        return db

    def partial(self, db, text="partial text", duration=100):
        return db.update_transcript("lecture", text, partial=True,
                                    received_seconds=duration, expected_seconds=300)

    def test_partial_can_be_emailed_and_stays_retryable(self):
        self.partial(self.db)
        self.db.update_error("lecture", "transcribe", "download interrupted")
        self.db.update_summary("lecture", "Partial notes", "model")
        self.assertEqual(len(self.db.get_unsent_lectures()), 1)
        self.db.mark_emailed("lecture")
        self.assertEqual(self.db.get_unsent_lectures(), [])
        self.assertEqual(len(self.db.get_unprocessed_lectures("course")), 1)
        self.assertFalse(LectureRunner._has_summary(self.db.get_lecture("lecture")))

    def test_longer_and_complete_results_replace_notes_and_email_state(self):
        self.partial(self.db)
        self.db.update_summary("lecture", "Old notes", "model")
        self.db.mark_emailed("lecture")
        self.assertFalse(self.partial(self.db, "shorter", 50))
        self.assertEqual(self.db.get_lecture("lecture")["summary"], "Old notes")
        self.assertTrue(self.partial(self.db, "longer", 150))
        row = self.db.get_lecture("lecture")
        self.assertIsNone(row["summary"])
        self.assertIsNone(row["emailed_at"])
        self.db.update_transcript("lecture", "Complete lecture")
        self.assertFalse(self.partial(self.db, "partial", 200))
        row = self.db.get_lecture("lecture")
        self.assertEqual(row["transcript"], "Complete lecture")
        self.assertFalse(row["transcript_partial"])

    def runner(self, transcriber):
        scheduler = Mock()
        scheduler.audio_downloader.get.return_value = Mock()
        return LectureRunner(Mock(), self.db, scheduler, transcriber, Mock(), Mock())

    def test_truncated_asr_is_saved_with_notice_and_summarized(self):
        transcriber = Mock()
        transcriber.transcribe_tail.side_effect = IncompleteAudioError(
            "truncated", 100, 300, "Recognized speech", [{"start_ms": 0, "end_ms": 1000, "text": "Recognized speech"}])
        runner = self.runner(transcriber)
        runner._summarizer.summarize.return_value = ("Notes", "model")
        text, segments = runner._get_transcript(None, "course", "lecture")
        self.assertIn("Recognized speech", text)
        self.assertTrue(self.db.get_lecture("lecture")["transcript_partial"])
        result = runner._summarize("lecture", "Course", text, segments)
        self.assertIn("音频不完整", result)
        self.assertIn("不补写缺失录音", runner._summarizer.summarize.call_args.args[1])
        self.assertIsNone(self.db.get_lecture("lecture")["processed_at"])

    def test_empty_partial_is_not_saved_or_marked_complete(self):
        transcriber = Mock()
        transcriber.transcribe_tail.side_effect = IncompleteAudioError("truncated", 100, 300, "")
        runner = self.runner(transcriber)
        self.assertEqual(runner._get_transcript(None, "course", "lecture"), (None, None))
        self.assertIsNone(self.db.get_lecture("lecture")["transcript"])

    def test_partial_run_sends_once_then_complete_retry_sends_update(self):
        lecture = {"sub_id": "lecture", "sub_title": "Lecture", "date": "2026-10-08"}
        transcriber = Mock()
        transcriber.transcribe_tail.side_effect = IncompleteAudioError("truncated", 100, 300, "Available speech")
        runner = self.runner(transcriber)
        runner._ppt = Mock()
        runner._summarizer.summarize.return_value = ("Partial notes", "model")
        runner.run("course", "Course", lecture)
        row = self.db.get_lecture("lecture")
        self.assertIsNone(row["processed_at"])
        self.assertEqual(row["error_stage"], "transcribe")
        emailer = Mock()
        emailer.send.return_value = True
        main._send_email(emailer, self.db, Mock(), [])
        main._send_email(emailer, self.db, Mock(), [])
        self.assertEqual(emailer.send.call_count, 1)
        transcriber = Mock()
        transcriber.transcribe_tail.return_value = ("Complete speech", [])
        runner = self.runner(transcriber)
        runner._ppt = Mock()
        runner._summarizer.summarize.return_value = ("Full notes", "model")
        runner.run("course", "Course", lecture)
        self.assertIsNotNone(self.db.get_lecture("lecture")["processed_at"])
        self.assertIsNone(self.db.get_lecture("lecture")["error_stage"])
        main._send_email(emailer, self.db, Mock(), [])
        self.assertEqual(emailer.send.call_count, 2)
        self.assertEqual(emailer.send.call_args.args[0][0]["summary"], "Full notes")

    def test_merge_never_downgrades_full_remote_transcript(self):
        remote = self.database("remote")
        try:
            remote.update_transcript("lecture", "Complete lecture")
            remote.update_summary("lecture", "Full notes", "model")
            remote.mark_processed("lecture")
            remote.mark_emailed("lecture")
            self.partial(self.db)
            merge(self.db.db_path, remote.db_path)
            row = remote.get_lecture("lecture")
            self.assertEqual(row["transcript"], "Complete lecture")
            self.assertEqual(row["summary"], "Full notes")
            self.assertIsNotNone(row["emailed_at"])
        finally:
            remote.conn.close()

    def test_merge_upgrade_invalidates_old_partial_notes(self):
        remote = self.database("remote")
        try:
            self.partial(remote)
            remote.update_summary("lecture", "Old partial notes", "model")
            remote.mark_emailed("lecture")
            self.db.update_transcript("lecture", "Complete lecture")
            merge(self.db.db_path, remote.db_path)
            row = remote.get_lecture("lecture")
            self.assertEqual(row["transcript"], "Complete lecture")
            self.assertIsNone(row["summary"])
            self.assertIsNone(row["emailed_at"])
            self.assertFalse(row["transcript_partial"])
        finally:
            remote.conn.close()

    def test_merge_and_encrypted_roundtrip_keep_longest_partial(self):
        remote = self.database("remote")
        try:
            self.partial(remote, "longer", 200)
            self.partial(self.db, "shorter", 100)
            merge(self.db.db_path, remote.db_path)
            self.assertEqual(remote.get_lecture("lecture")["transcript"], "longer")
            shards = str(Path(self.temp.name) / "shards")
            restored = str(Path(self.temp.name) / "restored.db")
            index = shard_database(remote.db_path, shards, "password")
            reassemble_database(index, str(Path(shards) / "shards"), restored, "password")
            restored_db = Database(restored)
            try:
                row = restored_db.get_lecture("lecture")
                self.assertTrue(row["transcript_partial"])
                self.assertEqual(row["audio_received_seconds"], 200)
            finally:
                restored_db.conn.close()
        finally:
            remote.conn.close()

    def test_download_error_carries_recognized_text_without_url_leak(self):
        transcriber = Transcriber()
        transcriber._vad = Mock()
        reads = iter([np.zeros(16000, dtype=np.float32).tobytes(), b""])
        def recognize(segments):
            if not segments:
                segments.append({"start_ms": 0, "end_ms": 1000, "text": "Available speech"})
        with patch.object(transcriber, "_init"), patch.object(transcriber, "_reset_vad"), \
                patch.object(transcriber, "_drain_segments", side_effect=recognize):
            with self.assertRaises(IncompleteAudioError) as caught:
                transcriber._consume_pcm_stream(lambda n: next(reads), lambda: True,
                    lambda: b"Duration: 00:00:03.00\nhttps://private/video.mp4?t=secret: Input/output error", lambda: 1, 60)
        self.assertEqual(caught.exception.transcript, "Available speech")
        self.assertIn("connection_error", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))


class VideoURLTests(unittest.TestCase):
    def test_open_time_and_account_visibility_are_recorded_separately(self):
        for code, info, expected in ((7001, {}, "未到开放时间"), (0, {"can_watch": False}, "can_watch=false")):
            client = ICourseClient(Mock())
            client._sub_info_codes["lecture"] = code
            with patch.object(client, "get_sub_info", return_value=info), \
                    patch.object(client, "get_sub_detail", return_value={}):
                self.assertIsNone(client.get_video_url("course", "lecture"))
            self.assertIn(expected, client.video_unavailable_reason("lecture"))

    def test_map_list_and_query_urls_are_supported(self):
        url = "https://example.test/lecture.mp4?token=placeholder"
        for body in ({"video_list": [{"preview_url": url}]}, {"playurl": [url]},
                     {"content": {"playback": {"url": url}}}):
            self.assertEqual(ICourseClient._video_candidates(body), [url])

    def test_non_media_urls_are_not_treated_as_recordings(self):
        self.assertEqual(ICourseClient._video_candidates({"video_list": ["https://example.test/page"],
            "content": {"webp_path": "https://example.test/1.webp"}}), [])

    def test_alternate_detail_shape_is_used_when_primary_has_no_media(self):
        client = ICourseClient(Mock())
        url = "https://example.test/lecture.mp4?token=placeholder"
        with patch.object(client, "get_sub_info", return_value={}), \
                patch.object(client, "get_sub_detail", return_value={"playurl": [url]}), \
                patch.object(client, "sign_video_url", return_value="signed") as sign:
            self.assertEqual(client.get_video_url("course", "lecture"), "signed")
        sign.assert_called_once_with(url, now=None)
