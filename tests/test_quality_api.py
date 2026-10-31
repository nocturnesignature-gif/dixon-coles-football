import unittest
from unittest.mock import patch
from api.quality import app


class QualityApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    @patch('api.quality.read_view')
    def test_counts_and_missing_references(self, read):
        read.side_effect = [[{'evaluated_forecasts': 23, 'distinct_matches': 21}], [{'scope': 'overall', 'market_log_loss': None}]]
        response = self.client.get('/api/prediction-quality')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['counts']['distinct_matches'], 21)
        self.assertIsNone(response.json['summary'][0]['market_log_loss'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    @patch('api.quality.read_view', side_effect=RuntimeError('secret must not leak'))
    def test_missing_config(self, read):
        response = self.client.get('/api/prediction-quality')
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('secret', response.get_data(as_text=True))

    @patch('api.quality.read_view', side_effect=TimeoutError())
    def test_timeout(self, read):
        self.assertEqual(self.client.get('/api/prediction-quality').status_code, 502)


if __name__ == '__main__':
    unittest.main()
