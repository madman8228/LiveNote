import argparse
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import livenote_codex_bridge as bridge


class CodexBridgeTests(unittest.TestCase):
    def test_summary_schema_requires_every_question_property(self) -> None:
        schema = json.loads(Path(bridge.summary_schema_path()).read_text(encoding='utf-8'))
        question = schema['properties']['questions']['items']
        self.assertEqual(set(question['required']), set(question['properties']))

    def test_run_codex_writes_prompt_as_utf8(self) -> None:
        captured = {}

        class FailedProcess:
            returncode = 1
            stdout = ''
            stderr = 'input is not valid UTF-8'

        def fake_run(*args, **kwargs):
            captured.update(kwargs)
            return FailedProcess()

        with patch.object(bridge.subprocess, 'run', side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                bridge.run_codex('中文 Prompt')

        self.assertEqual(captured['input'], '中文 Prompt')
        self.assertTrue(captured['text'])
        self.assertEqual(captured['encoding'], 'utf-8')
        self.assertEqual(captured['errors'], 'replace')

    def test_summary_failure_is_reported_to_server(self) -> None:
        args = argparse.Namespace(server='https://example.test/api/v1', task_id=None, limit=1, model=None)
        failure = RuntimeError('Codex stdin 编码失败')

        with patch.object(bridge, 'find_tasks', return_value=['task-1']), \
             patch.object(bridge, 'process_one', side_effect=failure), \
             patch.object(bridge, 'request_json', return_value={'ok': True}) as request_json, \
             patch.object(bridge, 'report_codex'):
            result = bridge.run_once(args)

        self.assertEqual(result, 1)
        request_json.assert_called_once_with(
            args.server,
            '/tasks/task-1/summary-failed',
            'POST',
            {'errorMessage': 'Codex stdin 编码失败'},
        )


if __name__ == '__main__':
    unittest.main()
