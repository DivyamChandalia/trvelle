import unittest
from trvelle.utils.travel_text import readable_text, readable_trip, itinerary_chat_text
from trvelle.tools.itinerary_tool import PlannedItem


class TravelTextTests(unittest.TestCase):
    def test_joined_clock_times_get_spaces_without_changing_the_schedule(self):
        self.assertEqual(readable_text('Prebook taxi, aim airport by07:30 for09:50 flight.'), 'Prebook taxi, aim airport by 07:30 for 09:50 flight.')
        self.assertEqual(readable_text('Leave at9:50am or07:30for09:50.'), 'Leave at 9:50 am or 07:30 for 09:50.')
        for text in ('2027-03-20T09:50:00+02:00', '2027-03-20T09:50Z', 'https://example.com/by09:50', 'Flight 0950', 'at27:30'):
            self.assertEqual(readable_text(text), text)

    def test_saved_and_generated_descriptions_are_readable_without_mutating_raw_plan(self):
        raw = {'daily_plan': [{'items': [{'title': 'Airport transfer', 'description': 'Arrive by07:30.', 'start_time': '07:30', 'visitor_details': {'booking': 'Book for09:50.', 'source_url': 'https://example.com/by09:50'}}]}]}
        item = readable_trip(raw)['daily_plan'][0]['items'][0]
        self.assertEqual(item['description'], 'Arrive by 07:30.')
        self.assertEqual(item['visitor_details']['booking'], 'Book for 09:50.')
        self.assertEqual(item['start_time'], '07:30')
        self.assertEqual(raw['daily_plan'][0]['items'][0]['description'], 'Arrive by07:30.')
        self.assertEqual(PlannedItem(title='Transfer', description='Arrive by07:30.').description, 'Arrive by 07:30.')

    def test_completion_summary_describes_finished_route_without_search_commentary(self):
        trip = {'summary': {'travelers': 2}, 'daily_plan': [{'destination': 'Rome — Monday 15 March'}, {'destination': 'Rome'}, {'destination': 'Florence'}]}
        self.assertEqual(itinerary_chat_text(trip), 'Your 3-day Rome → Florence itinerary for 2 travelers is ready.')
        self.assertIn('draft for 2 travelers is saved', itinerary_chat_text({**trip, 'planning_status': 'partial'}))
        self.assertEqual(itinerary_chat_text({}), 'Your itinerary is saved.')
        route = {'summary': {'origin': 'Bengaluru (BLR), India', 'travelers': 2}, 'daily_plan': [
            {'destination':'Rome — Monday'}, {'destination':'Rome → Florence — Tuesday'}, {'destination':'Florence → Bengaluru — Wednesday'}]}
        self.assertEqual(itinerary_chat_text(route), 'Your 3-day Rome → Florence itinerary for 2 travelers is ready.')
