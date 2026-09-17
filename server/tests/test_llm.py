import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from unittest.mock import patch

from server import llm


class LlmTests(unittest.TestCase):
    def test_json_extraction_accepts_surrounding_text(self) -> None:
        result = llm._extract_json('好的，以下是总结：\n{"title":"主题","overview":"概览"}\n以上。')
        self.assertEqual(result, {'title': '主题', 'overview': '概览'})

    def test_openai_compatible_http_response_is_normalised(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                length = int(self.headers.get('Content-Length', '0'))
                requests.append({
                    'path': self.path,
                    'body': json.loads(self.rfile.read(length).decode('utf-8')),
                    'authorization': self.headers.get('Authorization'),
                })
                payload = {
                    'choices': [{
                        'message': {
                            'content': json.dumps({
                                'title': '直播主题',
                                'overview': '内容概览',
                                'keyPoints': ['知识点一'],
                                'knowledgeStructure': [{'title': '基础', 'points': ['要点']}],
                                'questions': [{'question': '问题', 'answer': '答案', 'startMs': 1200}],
                                'actionItems': ['待办'],
                                'entities': ['工具'],
                                'confidenceNotes': ['来自测试模型'],
                            }, ensure_ascii=False),
                        },
                    }],
                }
                response = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(llm, 'LLM_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1'), patch.object(llm, 'LLM_MODEL', 'local-test'), patch.object(llm, 'LLM_API_KEY', ''):
                result = llm._request_summary(llm._summary_system(), {'transcript': '测试文本'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['path'], '/v1/chat/completions')
        self.assertIsNone(requests[0]['authorization'])
        self.assertEqual(requests[0]['body']['model'], 'local-test')
        self.assertEqual(result['title'], '直播主题')
        self.assertEqual(result['knowledgeStructure'][0]['points'], ['要点'])
        self.assertEqual(result['questions'][0]['startMs'], 1200)

    def test_openai_compatible_content_parts_are_joined(self) -> None:
        payload = json.dumps({
            'choices': [{
                'message': {
                    'content': [
                        {'type': 'text', 'text': '{"title":"分'},
                        {'type': 'text', 'text': '段","overview":"内容"}'},
                    ],
                },
            }],
        }).encode('utf-8')

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return

            def read(self) -> bytes:
                return payload

        with patch.object(llm, 'LLM_BASE_URL', 'http://local.test/v1'), patch.object(llm, 'LLM_MODEL', 'local-test'), patch.object(llm, 'LLM_API_KEY', ''), patch.object(llm.urllib.request, 'urlopen', return_value=Response()):
            result = llm._request_summary(llm._summary_system(), {'transcript': '测试文本'})

        self.assertEqual(result['title'], '分段')
        self.assertEqual(result['overview'], '内容')

    def test_local_openai_compatible_endpoint_does_not_require_api_key(self) -> None:
        with patch.object(llm, 'LLM_BASE_URL', 'http://127.0.0.1:11434/v1'), patch.object(llm, 'LLM_MODEL', 'qwen2.5:7b'), patch.object(llm, 'LLM_API_KEY', ''):
            self.assertTrue(llm.is_configured())

    def test_oversized_timeline_item_does_not_reorder_previous_text(self) -> None:
        segments = [
            {'index': 0, 'startMs': 0, 'endMs': 1000, 'text': '前置内容'},
            {'index': 1, 'startMs': 1000, 'endMs': 2000, 'text': '后续内容' * 10},
        ]
        chunks = llm._chunk_transcript(segments, '前置内容后续内容', 20)

        self.assertEqual(chunks[0]['transcript'], '前置内容')
        self.assertTrue(chunks[1]['transcript'].startswith('后续内容'))

    def test_long_transcript_is_summarized_in_parts_then_merged(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_request(system: str, prompt: dict) -> dict:
            calls.append((system, prompt))
            return {
                'title': '局部总结',
                'overview': '局部内容',
                'keyPoints': ['知识点'],
                'questions': [],
                'actionItems': [],
                'entities': [],
                'confidenceNotes': [],
            }

        segments = [
            {'index': index, 'startMs': index * 1000, 'endMs': (index + 1) * 1000, 'text': '中文内容 ' * 2000}
            for index in range(80)
        ]
        transcript = {'text': ''.join(item['text'] for item in segments), 'segments': segments}
        with patch.object(llm, 'is_configured', return_value=True), patch.object(llm, '_request_summary', side_effect=fake_request):
            result = llm.generate_summary({'title': '长直播'}, transcript, [])

        self.assertIsNotNone(result)
        self.assertGreater(len(calls), 2)
        self.assertIn('partialSummaries', calls[-1][1])
        self.assertNotIn('partialSummaries', calls[0][1])
        self.assertLessEqual(len(calls[0][1]['transcript']), llm.LLM_SUMMARY_CHUNK_CHARS)

    def test_short_transcript_stays_single_pass(self) -> None:
        calls: list[dict] = []

        def fake_request(system: str, prompt: dict) -> dict:
            calls.append(prompt)
            return {'title': '', 'overview': '', 'keyPoints': [], 'questions': [], 'actionItems': [], 'entities': [], 'confidenceNotes': []}

        with patch.object(llm, 'is_configured', return_value=True), patch.object(llm, '_request_summary', side_effect=fake_request):
            llm.generate_summary({'title': '短直播'}, {'text': '一段短内容', 'segments': []}, [])

        self.assertEqual(len(calls), 1)
        self.assertIn('transcript', calls[0])
        self.assertNotIn('partialSummaries', calls[0])


if __name__ == '__main__':
    unittest.main()
