import unittest

from server.content import build_structured_report


class ContentTests(unittest.TestCase):
    def test_report_preserves_long_audio_processing_metadata(self) -> None:
        report = build_structured_report(
            {
                'id': 'session-content',
                'title': '长音频',
                'started_at': 1,
                'ended_at': 2,
                'status': 'COMPLETED',
                'duration_ms': 600_000,
            },
            [],
            {
                'model': 'medium',
                'device': 'cpu',
                'language': 'zh',
                'text': '测试',
                'segments': [],
                'chunked': True,
                'chunkDurationSeconds': 300,
                'chunkOverlapSeconds': 1,
            },
            3,
        )

        self.assertTrue(report['transcript']['chunked'])
        self.assertEqual(report['transcript']['chunkDurationSeconds'], 300)
        self.assertEqual(report['transcript']['chunkOverlapSeconds'], 1)


if __name__ == '__main__':
    unittest.main()
