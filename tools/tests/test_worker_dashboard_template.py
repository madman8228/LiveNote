from pathlib import Path
import unittest


class WorkerDashboardTemplateTests(unittest.TestCase):
    def test_task_details_omit_redundant_result_row_but_keep_summary_and_error(self) -> None:
        template = (Path(__file__).resolve().parents[1] / 'livenote_worker_dashboard.html').read_text(encoding='utf-8')

        self.assertNotIn('<span class="event-detail-label">结果</span>', template)
        self.assertNotIn('.event-result {', template)
        self.assertIn('查看结构化结果', template)
        self.assertIn('event-error', template)


if __name__ == '__main__':
    unittest.main()
