"""Replay real acceptance evidence with no model calls or paid search requests."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from trvelle.tools.search_gateway import SearchGateway
from trvelle.tools.itinerary_tool import Itinerary
from trvelle.utils.itinerary_validation import validate_trip

FIXTURES = Path(__file__).parent / 'fixtures'


class AcceptanceReplayTests(unittest.TestCase):
    def test_recorded_flights_hotels_and_sources_need_no_credentials(self):
        transport = Mock(side_effect=AssertionError('Replay must not contact a provider'))
        gateway = SearchGateway(transport=transport)
        manifest = json.loads((FIXTURES / 'search/manifest.json').read_text())
        images, flight_results, web_results = [], 0, 0
        with patch.dict(os.environ, {'TRVELLE_SEARCH_MODE': 'replay', 'TRVELLE_REPLAY_DIR': str(FIXTURES / 'search'), 'SERPAPI_API_KEY': '', 'TAVILY_API_KEY': ''}):
            for entry in manifest['queries']:
                result = gateway.request(entry['provider'], entry['query'])
                self.assertTrue(result['_trvelle_search']['stale'])
                if entry['query'].get('engine') == 'google_flights':
                    flight_results += bool(result.get('best_flights') or result.get('other_flights'))
                images.extend(photo for hotel in result.get('properties', []) for photo in hotel.get('images', []))
                web_results += len(result.get('results', []))
        self.assertGreaterEqual(flight_results, 2)
        self.assertTrue(images)
        self.assertGreater(web_results, 0)
        transport.assert_not_called()

    def test_live_itinerary_preserves_times_and_flags_room_and_budget_constraints(self):
        trip = json.loads((FIXTURES / 'acceptance/singapore-itinerary.json').read_text())
        parsed = Itinerary.model_validate(trip).model_dump(mode='json')
        activities = [item for day in parsed['daily_plan'] for item in day['items'] if item['card_type'] == 'activity']
        self.assertEqual(len(parsed['daily_plan']), 5)
        self.assertGreaterEqual(len(activities), 6)
        self.assertTrue(all(item['start_time'] and item['end_time'] and item['location'] for item in activities))
        report = validate_trip(trip)
        self.assertEqual(report['priced_total'], 173988)
        self.assertEqual(report['budget_amount'], 180000)
        self.assertTrue({'room_unverified', 'bed_mismatch', 'unpriced_budget'}.issubset({issue['code'] for issue in report['issues']}))
