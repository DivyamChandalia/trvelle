import unittest
from trvelle.utils.flight_routes import with_airport_cities


class FlightRouteTests(unittest.TestCase):
    def test_city_metadata_matches_airport_ids_and_preserves_cached_quote(self):
        option = {'flights': [
            {'departure_airport': {'id': 'BOM'}, 'arrival_airport': {'id': 'BKK'}},
            {'departure_airport': {'id': 'BKK'}, 'arrival_airport': {'id': 'NRT'}},
        ]}
        airports = [{'departure': [{'airport': {'id': 'BOM'}, 'city': 'Mumbai'}],
                     'arrival': [{'airport': {'id': 'HND'}, 'city': 'Tokyo'}, {'airport': {'id': 'NRT'}, 'city': 'Tokyo'}]}]
        resolved = with_airport_cities(option, airports)
        self.assertEqual(resolved['flights'][0]['departure_airport']['city'], 'Mumbai')
        self.assertEqual(resolved['flights'][-1]['arrival_airport']['city'], 'Tokyo')
        self.assertNotIn('city', resolved['flights'][0]['arrival_airport'])
        self.assertNotIn('city', option['flights'][0]['departure_airport'])
        self.assertEqual(with_airport_cities(option, None), option)
