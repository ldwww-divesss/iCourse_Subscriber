"""Read course playback metadata without downloading media or writing data."""
import contextlib
import io
import json
import os
import time
from collections import Counter

from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession


def login():
    for attempt in range(3):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                vpn = WebVPNSession()
                vpn.login()
                vpn.authenticate_icourse()
            return vpn
        except Exception as exc:
            print(json.dumps({'login_attempt': attempt + 1, 'error_type': type(exc).__name__}), flush=True)
            if attempt == 2:
                raise
            time.sleep(5)


def request(client, path, params):
    for attempt in range(2):
        try:
            response = client.vpn.get(client.base_url + path, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt == 1:
                raise
            print(json.dumps({'retry': path.rsplit('/', 1)[-1], 'error_type': type(exc).__name__}), flush=True)
            client.vpn = login()
            client._userinfo = None


def url_count(value):
    if isinstance(value, dict):
        return sum(url_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(url_count(v) for v in value)
    return int(isinstance(value, str) and value.startswith(('https://', 'http://')))


def main():
    course_ids = [value.strip() for value in
                  os.environ.get('DIAGNOSTIC_COURSE_IDS', '').split(',') if value.strip()]
    if not course_ids or not all(value.isdigit() for value in course_ids):
        raise SystemExit('Configure COURSE_IDS or provide numeric course IDs')
    sub_ids = {value.strip() for value in os.environ.get('DIAGNOSTIC_SUB_IDS', '').split(',') if value.strip()}
    if not all(value.isdigit() for value in sub_ids):
        raise SystemExit('Provide numeric lecture IDs')
    client = ICourseClient(login())
    for course_id in course_ids:
        response = request(client, '/courseapi/v3/multi-search/get-course-detail', {'course_id': course_id})
        if response.get('code') != 0:
            print(json.dumps({'course_id': course_id, 'course_api_code': response.get('code')}), flush=True)
            continue
        data = response.get('data') or {}
        items = [item for item, _ in ICourseClient._lecture_items(data.get('sub_list'))]
        print(json.dumps({'course_id': course_id, 'lecture_count': len(items),
                          'playback_status_counts': dict(Counter(str(item.get('playback_status')) for item in items))}), flush=True)
        # Sample first, middle and last lectures to limit diagnostic traffic.
        samples = [items[i] for i in sorted({0, len(items) // 2, len(items) - 1})] if items else []
        if sub_ids:
            samples = [item for item in items if str(item['id']) in sub_ids]
        for item in samples:
            result = {'course_id': course_id, 'sub_id': item['id'], 'date': item.get('sub_title', '')[:10],
                      'playback_status': item.get('playback_status')}
            for name, path in (
                ('info', '/courseapi/v3/portal-home-setting/get-sub-info'),
                ('detail', '/courseapi/v3/multi-search/get-sub-detail'),
            ):
                try:
                    payload = request(client, path, {'course_id': course_id, 'sub_id': item['id']})
                    result[name + '_api_code'] = payload.get('code')
                    message = payload.get('msg')
                    if isinstance(message, str) and 'http' not in message.lower() and len(message) <= 120:
                        result[name + '_message'] = message
                    # Nonzero responses can still contain playback metadata.
                    # Emit shapes and counts only, never URLs or credentials.
                    if isinstance(payload.get('data'), dict):
                        body = payload.get('data') or {}
                        content = body.get('content') or {}
                        playback = content.get('playback') or {}
                        result[name + '_video_list_urls'] = url_count(body.get('video_list'))
                        result[name + '_playurl_urls'] = url_count(body.get('playurl'))
                        result[name + '_playback_url_present'] = bool(playback.get('url')) if isinstance(playback, dict) else False
                        result[name + '_data_keys'] = sorted(body)
                        result[name + '_content_keys'] = sorted(content) if isinstance(content, dict) else []
                        result[name + '_playback_keys'] = sorted(playback) if isinstance(playback, dict) else []
                        result[name + '_url_count'] = url_count(body)
                        result[name + '_video_list_type'] = type(body.get('video_list')).__name__
                        for field in ('status', 'playback_status', 'is_open', 'open_time', 'review_status',
                                      'can_watch', 'play_msg', 'sub_status', 'sub_review_type',
                                      'replay_open_at', 'duration', 'is_m3u8'):
                            value = body.get(field)
                            if isinstance(value, (int, bool)) or (isinstance(value, str) and len(value) < 40 and 'http' not in value.lower()):
                                result[name + '_' + field] = value
                        for field in ('download_url', 'file_list', 'resource', 'trans_socket_url', 'webp_path'):
                            result[name + '_content_' + field + '_urls'] = url_count(content.get(field))
                        for field in ('save_playback', 'process_type', 'start_at', 'end_at'):
                            value = content.get(field)
                            if isinstance(value, (int, bool)) or (isinstance(value, str) and len(value) < 40 and 'http' not in value.lower()):
                                result[name + '_content_' + field] = value
                except Exception as exc:
                    result[name + '_error_type'] = type(exc).__name__
                time.sleep(0.3)
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
