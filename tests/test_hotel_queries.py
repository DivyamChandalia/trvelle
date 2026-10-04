import unittest
from unittest.mock import Mock
from trvelle.tools.hotel_search import HotelSearch


class HotelQueryTests(unittest.TestCase):
    def setUp(self):
        self.searcher = HotelSearch()
        self.searcher.api_key = 'test-key'
        self.searcher._fetch_results = Mock(return_value={'properties':[]})

    def test_each_city_query_survives_a_combined_research_scope(self):
        for city in ('Rome', 'Florence'):
            with self.subTest(city=city):
                result = self.searcher.hotel_search({'q':f'{city} Italy near public transport',
                    '_destination':'Rome and Florence', 'check_in_date':'2027-03-14',
                    'check_out_date':'2027-03-17', 'currency':'INR', 'adults':2, 'rating':8})
                params = self.searcher._fetch_results.call_args.args[0]
                self.assertEqual(params['q'],f'{city} Italy')
                self.assertEqual(params['rating'],8)
                self.assertEqual(result['raw']['stay_context']['destination'],f'{city} Italy')

    def test_explicit_property_query_is_never_replaced_by_research_scope(self):
        query = 'Hotel Brunelleschi Florence'
        self.searcher.hotel_search({'q':query, '_destination':'Rome',
            'check_in_date':'2027-03-17','check_out_date':'2027-03-20'})
        self.assertEqual(self.searcher._fetch_results.call_args.args[0]['q'],query)

    def test_neighborhood_preferences_do_not_turn_the_city_into_a_sentence(self):
        for query,city in [('Rome near Termini public transport','Rome'),
                           ('Florence Italy near Santa Maria Novella SMN station','Florence Italy')]:
            self.searcher.hotel_search({'q':query,'_destination':city,
                'check_in_date':'2027-03-14','check_out_date':'2027-03-17'})
            self.assertEqual(self.searcher._fetch_results.call_args.args[0]['q'],city)

    def test_model_evidence_reports_available_provider_photos(self):
        text=self.searcher.format_hotel_data_simple([{'name':'Verified hotel',
            'images':[{'original_image':'https://example.test/actual-hotel.jpg'}]}])
        self.assertIn('1 provider images are available',text)
        self.assertIn('https://example.test/actual-hotel.jpg',text)

    def test_same_property_has_distinct_offer_ids_for_different_dates(self):
        from copy import deepcopy
        self.searcher._fetch_results=Mock(side_effect=lambda *args:deepcopy({'properties':[{'name':'Rome Hotel','property_token':'property'}]}))
        def search(start,end):
            return self.searcher.hotel_search({'q':'Rome','check_in_date':start,'check_out_date':end,'adults':2})
        first=search('2027-03-15','2027-03-17')
        repeat=search('2027-03-15','2027-03-17')
        last=search('2027-03-19','2027-03-20')
        self.assertEqual(first['raw']['properties'][0]['choose_uid'],repeat['raw']['properties'][0]['choose_uid'])
        self.assertNotEqual(first['raw']['properties'][0]['choose_uid'],last['raw']['properties'][0]['choose_uid'])
        self.assertIn('2027-03-19 to 2027-03-20',last['result'])
