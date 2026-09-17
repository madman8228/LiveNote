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

    def test_unconfigured_report_has_extractable_draft_without_claiming_llm(self) -> None:
        report = build_structured_report(
            {
                'id': 'session-extractive',
                'title': '抽取式草稿',
                'started_at': 1,
                'ended_at': 2,
                'status': 'COMPLETED',
                'duration_ms': 30_000,
            },
            [],
            {
                'model': 'medium',
                'device': 'cpu',
                'language': 'zh',
                'text': '首先介绍录音流程。核心问题是网络恢复后不能停止录音。最后需要保留本地文件。',
                'segments': [
                    {'startMs': 0, 'endMs': 5000, 'text': '首先介绍录音流程。'},
                    {'startMs': 5000, 'endMs': 12_000, 'text': '核心问题是网络恢复后不能停止录音。'},
                    {'startMs': 12_000, 'endMs': 18_000, 'text': '最后需要保留本地文件。'},
                ],
            },
            3,
        )

        self.assertEqual(report['summaryStatus'], 'NOT_CONFIGURED')
        self.assertEqual(report['analysisProvider'], 'local-extractive-draft')
        self.assertGreaterEqual(len(report['localDraft']['keyPoints']), 1)
        self.assertTrue(report['localDraft']['knowledgeStructure'])
        structure_titles = {section['title'] for section in report['localDraft']['knowledgeStructure']}
        self.assertIn('方法与步骤', structure_titles)
        self.assertIn('问题与原因', structure_titles)


if __name__ == '__main__':
    unittest.main()
