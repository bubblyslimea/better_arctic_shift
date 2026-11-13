import json
import unittest
from pathlib import Path

from better_arctic_shift.history import capture_states, observation_rank


class SharedHistoryTests(unittest.TestCase):
    def test_captured_states(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/history-contract.json').read_text())
        for case in cases:
            with self.subTest(case['name']):
                self.assertEqual(capture_states(case['record'], case['kind'], case['source'], case['captured']), case['expected'])

    def test_wording_date_is_separate_from_status_date(self):
        earlier = {'source': 'arctic_shift', 'observed_at': '1970-01-01T00:10:00Z', 'source_time': '1970-01-01T00:05:00Z', 'content_time': '1970-01-01T00:01:40Z'}
        edited = {'source': 'reddit', 'observed_at': '1970-01-01T00:03:20Z'}
        self.assertGreater(observation_rank(earlier, 'present'), observation_rank(edited, 'present'))
        self.assertLess(observation_rank(earlier, 'present', True), observation_rank(edited, 'present', True))
        self.assertLess(observation_rank({**earlier, 'content_time': None}, 'present', True), observation_rank(edited, 'present', True))
