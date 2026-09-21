import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.analyze_playback_diagnostic import analyze


class PlaybackDiagnosticAnalysisTests(unittest.TestCase):
    def base_report(self) -> dict:
        return {
            'kind': 'playback-diagnostic', 'schemaVersion': 1,
            'clientReportId': 'client-1', 'sessionId': 'session-1', 'attemptId': 'attempt-1', 'buildId': 'test-build',
            'preparation': [], 'droppedEvents': 0,
            'events': [
                {'seq': 1, 'atMs': 0, 'type': 'play', 'data': {}},
                {'seq': 2, 'atMs': 500, 'type': 'media-sample', 'data': {'currentTime': 1, 'duration': 10, 'buffered': [{'start': 0, 'end': 10}]}},
                {'seq': 3, 'atMs': 1000, 'type': 'media-sample', 'data': {'currentTime': 1.5, 'duration': 10, 'buffered': [{'start': 0, 'end': 10}]}},
            ],
        }

    def test_position_rollback_is_reported_without_seek(self) -> None:
        report = self.base_report()
        report['events'].append({'seq': 4, 'atMs': 1500, 'type': 'media-sample', 'data': {'currentTime': 0.2, 'duration': 10, 'buffered': [{'start': 0, 'end': 10}]}})
        result = analyze(report)
        self.assertEqual(len(result['evidence']['positionRollbacks']), 1)
        self.assertTrue(any('回退' in item for item in result['candidates']))

    def test_seek_nearby_is_not_called_an_unexpected_rollback(self) -> None:
        report = self.base_report()
        report['events'].extend([
            {'seq': 4, 'atMs': 1400, 'type': 'seeking', 'data': {}},
            {'seq': 5, 'atMs': 1500, 'type': 'media-sample', 'data': {'currentTime': 0.2, 'duration': 10, 'buffered': [{'start': 0, 'end': 10}]}},
        ])
        result = analyze(report)
        self.assertEqual(result['evidence']['positionRollbacks'], [])

    def test_duration_and_source_changes_are_separated(self) -> None:
        report = self.base_report()
        report['events'].extend([
            {'seq': 4, 'atMs': 1200, 'type': 'media-sample', 'data': {'currentTime': 2, 'duration': 20, 'buffered': [{'start': 0, 'end': 20}]}},
            {'seq': 5, 'atMs': 1300, 'type': 'source-assigned', 'data': {'mode': 'server-ffmpeg'}},
            {'seq': 6, 'atMs': 1400, 'type': 'source-assigned', 'data': {'mode': 'local'}},
        ])
        result = analyze(report)
        self.assertEqual(len(result['evidence']['durationChanges']), 1)
        self.assertEqual(result['evidence']['sourceChanges'], 2)

    def test_cli_reads_only_the_explicit_input(self) -> None:
        report = self.base_report()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            result = subprocess.run([sys.executable, 'tools/analyze_playback_diagnostic.py', '--input', str(path)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('client-1', result.stdout)


if __name__ == '__main__':
    unittest.main()
