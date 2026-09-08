"""Read course playback metadata without downloading media or writing data."""
import contextlib
import io
import json
import os
import time
from collections import Counter

from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession


def request(client, path, params):
    response = client.vpn.get(client.base_url + path, params=params)
    response.raise_for_status()
    return response.json()


def url_count(value):
    if isinstance(value, dict):
        return sum(url_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(url_count(v) for v in value)
    return int(isinstance(value, str) and value.startswith(('https://', 'http://')))


def main():
    vpn = None
    for attempt in range(3):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                vpn = WebVPNSession()
                vpn.login()
                vpn.authenticate_icourse()
            break
        except Exception as exc:
            print(json.dumps({'login_attempt': attempt + 1, 'error_type': type(exc).__name__}), flush=True)
            if attempt == 2:
                raise SystemExit(1)
            time.sleep(5)
    client = ICourseClient(vpn)
    for course_id in os.environ.get('DIAGNOSTIC_COURSE_IDS', '11551,38835').split(','):
        course_id = course_id.strip()
        if not course_id.isdigit():
            raise SystemExit('Course IDs must be numeric')
        response = request(client, '/courseapi/v3/multi-search/get-course-detail', {'course_id': course_id})
        if response.get('code') != 0:
            print(json.dumps({'course_id': course_id, 'course_api_code': response.get('code')}), flush=True)
            continue
        data = response.get('data') or {}
        items = [item for months in data.get('sub_list', {}).values()
                 for days in months.values() for entries in days.values() for item in entries if 'id' in item]
        print(json.dumps({'course_id': course_id, 'lecture_count': len(items),
                          'playback_status_counts': dict(Counter(str(item.get('playback_status')) for item in items))}), flush=True)
        for item in items:
            result = {'course_id': course_id, 'sub_id': item['id'], 'date': item.get('sub_title', '')[:10],
                      'playback_status': item.get('playback_status')}
            for name, path in (
                ('info', '/courseapi/v3/portal-home-setting/get-sub-info'),
                ('detail', '/courseapi/v3/multi-search/get-sub-detail'),
            ):
                try:
                    payload = request(client, path, {'course_id': course_id, 'sub_id': item['id']})
                    result[name + '_api_code'] = payload.get('code')
                    # Only inspect successful responses. Never sign or request media URLs.
                    if payload.get('code') == 0:
                        body = payload.get('data') or {}
                        content = body.get('content') or {}
                        playback = content.get('playback') or {}
                        result[name + '_video_list_urls'] = url_count(body.get('video_list'))
                        result[name + '_playurl_urls'] = url_count(body.get('playurl'))
                        result[name + '_playback_url_present'] = bool(playback.get('url')) if isinstance(playback, dict) else False
                except Exception as exc:
                    result[name + '_error_type'] = type(exc).__name__
                time.sleep(0.3)
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
