import os
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

import main
from src.ai.summarizer import Summarizer
from src.ai.transcriber import Transcriber
from src.api.icourse import ICourseClient
from src.data.database import Database
from src.pipeline.ppt_pipeline import PPTPipeline, PPTAsyncHandle
from src.runtime import config
from src.runtime.process_guard import run_with_timeout
from src.runtime.scheduler import PrefetchCache
from tests import worker_fixtures


def response(data):
    return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)


class CourseParsingTests(unittest.TestCase):
    def detail(self, sub_list):
        vpn = Mock()
        vpn.get.return_value = response({"code": 0, "data": {"sub_list": sub_list}})
        return ICourseClient(vpn).get_course_detail("course")["lectures"]

    def test_empty_and_missing_calendar(self):
        for value in ([], {}, None):
            with self.subTest(value=value):
                self.assertEqual(self.detail(value), [])

    def test_nested_calendar_retains_date_fallback(self):
        result = self.detail({"2024": {"9": {"5": [{"id": 1, "playback_status": 1}]}}})
        self.assertEqual(result[0]["date"], "2024-09-05")
        self.assertTrue(result[0]["has_playback"])

    def test_flat_list_prefers_title_date(self):
        result = self.detail([{"id": 2, "sub_title": "2026-09-28第6-9节"}, None])
        self.assertEqual(result[0]["date"], "2026-09-28")
        self.assertEqual(result[0]["sub_id"], 2)

    def test_nested_empty_lists_and_flat_undated_lecture(self):
        result = self.detail({"2026": {"9": []}, "extra": [{"id": 3}]})
        self.assertEqual(result[0]["date"], "")


class PaginationTests(unittest.TestCase):
    def client(self, pages):
        vpn = Mock()
        vpn.get.side_effect = [response({"code": 0, "list": p}) for p in pages]
        return ICourseClient(vpn)

    def item(self, i):
        return {"id": i, "created_sec": i,
                "content": '{"pptimgurl": "https://example.com/image.png"}'}

    def test_normal_multiple_pages(self):
        client = self.client([[self.item(2)], [self.item(1)], []])
        result = client.get_ppt_list("course", "lecture", per_page=1)
        self.assertEqual([x["id"] for x in result], [1, 2])

    def test_repeated_page_stops(self):
        client = self.client([[self.item(1)], [self.item(1)]])
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            client.get_ppt_list("course", "lecture", per_page=1)
        self.assertEqual(client.vpn.get.call_count, 2)

    def test_page_limit_stops_unique_endless_pages(self):
        client = self.client([[self.item(1)], [self.item(2)]])
        with patch.object(config, "PPT_MAX_PAGES", 1):
            with self.assertRaises(TimeoutError):
                client.get_ppt_list("course", "lecture", per_page=1)
        self.assertEqual(client.vpn.get.call_count, 1)

    def test_wall_clock_budget_stops(self):
        client = self.client([[self.item(1)], [self.item(2)]])
        with patch("src.api.icourse.time.monotonic", side_effect=[0, 0, 121]):
            with self.assertRaises(TimeoutError):
                client.get_ppt_list("course", "lecture", per_page=1)
        self.assertEqual(client.vpn.get.call_count, 1)


class SummaryTests(unittest.TestCase):
    def make_summarizer(self):
        providers = [{"name": "test", "api_key": "test-key",
                      "base_url": "https://example.invalid", "models": ["first", "backup"]}]
        with patch.object(config, "resolve_model_providers", return_value=providers), \
                patch("src.ai.summarizer.OpenAI") as factory:
            summarizer = Summarizer()
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        return summarizer, factory.return_value.chat.completions.create

    def test_invalid_response_uses_backup(self):
        invalid = [SimpleNamespace(choices=[])] + [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=x))])
            for x in (None, "", "   ")
        ]
        for result in invalid:
            with self.subTest(result=result):
                summarizer, call = self.make_summarizer()
                call.side_effect = [result, SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=" valid summary "))], usage=None)]
                self.assertEqual(summarizer.summarize("title", "text"), ("valid summary", "test/backup"))

    def test_provider_error_uses_backup(self):
        summarizer, call = self.make_summarizer()
        call.side_effect = [RuntimeError("no provider supported"), SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))], usage=None)]
        self.assertEqual(summarizer.summarize("title", "text")[1], "test/backup")

    def test_all_models_fail(self):
        summarizer, call = self.make_summarizer()
        call.side_effect = RuntimeError("provider unavailable")
        with self.assertRaisesRegex(RuntimeError, "All LLM models failed"):
            summarizer.summarize("title", "text")


class WaitTimeoutTests(unittest.TestCase):
    def test_image_wait_is_bounded(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            cache = PrefetchCache(pool)
            cache._cache["lecture"] = {"items": [], "futures": {1: Future()}}
            with patch.object(config, "PPT_WAIT_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(TimeoutError, "Image prefetch"):
                    cache.wait("lecture")

    def test_ocr_wait_is_bounded(self):
        handle = PPTAsyncHandle(Mock(), "lecture", total=1, inserted=0,
                                futures=[Future()], dedupped=0, presubmit_failed=0)
        with patch.object(config, "PPT_WAIT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                handle.drain()

    def test_prefetch_thread_wait_is_bounded(self):
        pipeline = PPTPipeline(Mock(), Mock(), Mock())
        thread = Mock()
        thread.is_alive.return_value = True
        pipeline._prefetch_threads["lecture"] = thread
        with self.assertRaisesRegex(TimeoutError, "prefetch thread"):
            pipeline._join_prefetch("lecture")
        thread.join.assert_called_once_with(timeout=config.PPT_WAIT_TIMEOUT_SECONDS)

    def test_audio_stall_exits_before_overall_timeout(self):
        transcriber = Transcriber()
        with patch.object(transcriber, "_init"), patch.object(transcriber, "_reset_vad"), \
                patch("src.ai.transcriber.time.time", side_effect=[0, 121]):
            with self.assertRaisesRegex(TimeoutError, "No audio data"):
                transcriber._consume_pcm_stream(
                    lambda n: b"", lambda: False, lambda: b"", lambda: None,
                    timeout=7200, wait_on_empty_sec=0.1,
                )


class WorkerTests(unittest.TestCase):
    def test_real_lecture_worker_saves_summary_via_local_api(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.request = request
                payload = json.dumps({
                    "id": "test", "object": "chat.completion", "created": 0,
                    "model": request["model"], "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": "Test course summary"},
                        "finish_reason": "stop"}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = str(Path(temp) / "db.sqlite")
                worker_fixtures.save_transcript(path)
                with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key",
                        "DASHSCOPE_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
                        "http_proxy": "", "https_proxy": "", "all_proxy": "",
                        "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                    self.assertEqual(run_with_timeout(main._process_lecture,
                        (worker_fixtures.EmptyPPTVPN(), path, "course", "Test course",
                         {"sub_id": "lecture", "sub_title": "Test lecture"}), 60), 0)
                db = Database(path)
                row = db.get_lecture("lecture")
                self.assertEqual(row["summary"], "Test course summary")
                self.assertIsNotNone(row["processed_at"])
                self.assertEqual(row["error_count"], 0)
                self.assertIn("committed transcript", server.request["messages"][1]["content"])
                db.conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_success_commits_database(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "db.sqlite")
            self.assertEqual(run_with_timeout(worker_fixtures.save_transcript, (path,), 10), 0)
            db = Database(path)
            self.assertEqual(db.get_lecture("lecture")["transcript"], "committed transcript")
            db.conn.close()

    def test_timeout_preserves_already_committed_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "db.sqlite")
            with self.assertRaises(TimeoutError):
                run_with_timeout(worker_fixtures.save_transcript, (path, 60), 3)
            db = Database(path)
            self.assertEqual(db.get_lecture("lecture")["transcript"], "committed transcript")
            db.conn.close()
            # A later task can still run after terminating the stuck worker.
            self.assertEqual(run_with_timeout(worker_fixtures.save_transcript, (path,), 10), 0)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_timeout_also_stops_download_child(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "pid")
            with self.assertRaises(TimeoutError):
                run_with_timeout(worker_fixtures.spawn_stuck_download, (path,), 3)
            pid = int(Path(path).read_text())
            deadline = time.monotonic() + 2
            while psutil.pid_exists(pid) and time.monotonic() < deadline:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    break
                time.sleep(0.05)
            self.assertTrue(not psutil.pid_exists(pid)
                            or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)

    def test_worker_failure_returns_nonzero(self):
        self.assertNotEqual(run_with_timeout(worker_fixtures.fail, (), 10), 0)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "db.sqlite"))
        self.db.upsert_course("course", "Course", "Teacher")
        self.lectures = [{"sub_id": "1", "sub_title": "First", "has_playback": True},
                         {"sub_id": "2", "sub_title": "Second", "has_playback": True}]
        for lecture in self.lectures:
            self.db.insert_lecture(lecture["sub_id"], "course", lecture["sub_title"], "")

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def test_failed_lecture_is_not_reintroduced_as_new(self):
        for _ in range(config.MAX_LECTURE_ERRORS):
            self.db.update_error("1", "timeout", "stuck")
        client = Mock()
        client.get_course_detail.return_value = {"title": "Course", "teacher": "Teacher",
                                                 "lectures": self.lectures}
        with patch.object(config, "COURSE_IDS", ["course"]), patch.object(main, "_check_session"):
            result = main._enumerate_lectures(client, self.db, Mock())
        self.assertEqual([x[2]["sub_id"] for x in result], ["2"])

    def test_timeout_records_error_and_continues(self):
        def worker(*args):
            if args[1][-1]["sub_id"] == "1":
                raise TimeoutError("stuck")
            self.db.update_summary("2", "second summary", "test/model")
            self.db.mark_processed("2")
            return 0
        items = []
        with patch.object(main, "_check_session"), patch.object(main, "run_with_timeout", side_effect=worker):
            main._drive_lectures(Mock(), self.db, Mock(),
                                 [("course", "Course", x) for x in self.lectures], items)
        self.assertEqual(self.db.get_lecture("1")["error_stage"], "timeout")
        self.assertEqual(self.db.get_lecture("1")["error_count"], 1)
        self.assertEqual([x["sub_id"] for x in items], ["2"])

    def test_run_budget_leaves_unstarted_lectures_untouched(self):
        with patch.object(config, "RUN_BUDGET_SECONDS", 0), patch.object(main, "run_with_timeout") as worker:
            main._drive_lectures(Mock(), self.db, Mock(), [("course", "Course", self.lectures[0])], [])
        worker.assert_not_called()
        self.assertEqual(self.db.get_lecture("1")["error_count"], 0)


if __name__ == "__main__":
    unittest.main()
