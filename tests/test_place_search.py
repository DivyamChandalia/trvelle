import unittest
from unittest.mock import AsyncMock, patch
from trvelle.tools.place_search import match_place,find_place,normalize_place,enrich_itinerary
from trvelle.tools.search_gateway import search_context,SearchBudgetError
from trvelle.tools.web_search import research_search,search_providers

def candidate(name='Uffizi Gallery',city='Florence',country='IT',photo=True):
    return {'id':'temporary','title':name,'url':'https://www.uffizi.it/',
        'provider_url':'https://example.test/florence/uffizi','coordinates':[43.7687,11.255],
        'postal_address':{'displayAddress':f'{city}, {country}','addressLocality':city,'country':country},
        'rating':{'ratingValue':4.7,'bestRating':5,'reviewCount':4000},
        **({'thumbnail':{'src':'https://example.test/uffizi.jpg'}} if photo else {})}

class PlaceTests(unittest.IsolatedAsyncioTestCase):
    def test_brave_is_the_default_and_developer_test_overrides_remain_available(self):
        with patch.dict('os.environ',{'TRVELLE_WEB_SEARCH_PROVIDER':'','TRVELLE_PLACE_SEARCH_PROVIDER':''}):
            self.assertEqual(search_providers(),{'web':'brave','places':'brave'})
            with search_context(providers={'web':'tavily','places':'disabled'}):
                self.assertEqual(search_providers(),{'web':'tavily','places':'disabled'})

    def test_name_and_location_are_both_required(self):
        wrong_city=candidate(city='Venice');wrong_name=candidate(name='Museum of Football')
        matched=match_place('Uffizi Gallery','Florence Italy',[wrong_city,wrong_name,candidate()])
        self.assertEqual(matched[0]['postal_address']['addressLocality'],'Florence')
        self.assertIsNone(match_place('Uffizi Gallery','Florence Italy',[wrong_city,wrong_name]))
        self.assertIsNone(match_place('Colosseum','Rome Italy',[candidate(name='Colosseum',city='Rome',country='US')]))
        self.assertIsNone(match_place('Uffizi Gallery','Florence Italy',[candidate(name='Bounce Luggage Storage - Uffizi Gallery')]))

    def test_local_city_names_match_and_ambiguous_branches_are_rejected(self):
        self.assertIsNotNone(match_place('Uffizi Gallery','Florence Italy',[candidate(city='Firenze')]))
        a,b=candidate(),candidate();a['postal_address']['displayAddress']='First street Florence IT';b['postal_address']['displayAddress']='Second street Florence IT'
        self.assertIsNone(match_place('Uffizi Gallery','Florence Italy',[a,b]))
        b['url']='https://other-business.test/'
        self.assertEqual(match_place('Uffizi Gallery','Florence Italy',[b,a],source_url='https://www.uffizi.it/en/')[0],a)

    def test_combined_visit_matches_one_exact_attraction_in_the_correct_city(self):
        forum=candidate(name='Foro Romano',city='Rome')
        palatine=candidate(name='Palatine Hill',city='Rome')
        matched=match_place('Roman Forum and Palatine Hill','Rome Italy',[palatine,forum])
        self.assertEqual(matched[0],forum)
        self.assertIsNone(match_place('Roman Forum and Palatine Hill','Rome Italy',[candidate(name='Foro Romano',city='Florence')]))

    def test_vatican_activity_address_overrides_rome_day_destination(self):
        museums=candidate(name='Vatican Museums',city='Vatican City',country='VA')
        chapel=candidate(name='Sistine Chapel',city='Vatican City',country='VA')
        matched=match_place('Vatican Museums and Sistine Chapel','Viale Vaticano, Vatican City',[chapel,museums],destination='Rome')
        self.assertEqual(matched[0],museums)
        basilica=candidate(name='Basilica di San Pietro in Vaticano',city='Città del Vaticano',country='VA')
        self.assertIsNotNone(match_place('St Peter’s Basilica and square','Piazza San Pietro, Vatican City',[basilica],destination='Rome'))
        self.assertIsNone(match_place('Vatican Museums','Vatican City',[candidate(name='Vatican Museums',city='Rome',country='IT')],destination='Rome'))

    def test_activity_city_overrides_multi_city_heading_and_local_name_suffixes(self):
        gallery=candidate(name='Galleria dell’Accademia di Firenze')
        matched=match_place('Accademia Gallery','Via Ricasoli, Florence',[gallery],destination='Rome → Florence')
        self.assertEqual(matched[0],gallery)

    def test_normalized_details_keep_provenance_and_drop_temporary_ids_and_logos(self):
        raw=candidate();raw['thumbnail']['logo']=True;raw['pictures']={'results':[{'thumbnail':{'src':'https://example.test/interior.jpg'},'url':'https://photo-source.test/uffizi'}]}
        place=normalize_place(raw,.9)
        self.assertNotIn('id',place)
        self.assertEqual(place['rating'],4.7)
        self.assertEqual(place['photos'][0]['source_url'],'https://photo-source.test/uffizi')
        self.assertEqual(len(place['photos']),1)

    async def test_existing_thumbnail_prevents_extra_poi_request(self):
        with patch('trvelle.tools.place_search.gateway.arequest',new=AsyncMock(return_value={'results':[candidate()]})) as request:
            place=await find_place('Uffizi Gallery','Florence Italy',photos=True)
        request.assert_awaited_once()
        self.assertEqual(place['photos'][0]['thumbnail'],'https://example.test/uffizi.jpg')

    async def test_missing_photo_triggers_one_matched_poi_lookup(self):
        raw=candidate(photo=False);details={**raw,'pictures':{'results':[{'src':'https://example.test/gallery.jpg'}]}}
        with patch('trvelle.tools.place_search.gateway.arequest',new=AsyncMock(side_effect=[{'results':[raw]},{'results':[details]}])) as request:
            place=await find_place('Uffizi Gallery','Florence Italy',photos=True)
        self.assertEqual(request.await_count,2)
        self.assertEqual(request.call_args.args[1]['endpoint'],'pois')
        self.assertEqual(place['photos'][0]['url'],'https://example.test/gallery.jpg')

    async def test_no_match_never_fetches_photos_and_no_photo_is_an_acceptable_result(self):
        with patch('trvelle.tools.place_search.gateway.arequest',new=AsyncMock(return_value={'results':[candidate(city='Venice')]})) as request:
            self.assertIsNone(await find_place('Uffizi Gallery','Florence Italy',photos=True))
        request.assert_awaited_once()
        with patch('trvelle.tools.place_search.gateway.arequest',new=AsyncMock(side_effect=[{'results':[candidate(photo=False)]},SearchBudgetError('Photo cap')])):
            place=await find_place('Uffizi Gallery','Florence Italy',photos=True)
        self.assertNotIn('photos',place)
        self.assertEqual(place['name'],'Uffizi Gallery')

    async def test_defaults_do_not_enrich_itineraries_and_enabled_enrichment_is_bounded(self):
        trip={'daily_plan':[{'destination':'Florence','items':[{'card_type':'activity','title':f'Museum {chr(65+index)}','location':'Florence'} for index in range(10)]}]}
        with search_context(providers={'places':'disabled'}),patch('trvelle.tools.place_search.find_place',new=AsyncMock()) as find:
            self.assertEqual(await enrich_itinerary(trip),trip)
        find.assert_not_awaited()
        with search_context(providers={'places':'brave'}),patch.dict('os.environ',{'BRAVE_ITINERARY_PLACE_LIMIT':'2'}),patch('trvelle.tools.place_search.find_place',new=AsyncMock(return_value=normalize_place(candidate(),1))) as find:
            result=await enrich_itinerary(trip)
        self.assertEqual(find.await_count,2)
        self.assertIn('image_url',result['daily_plan'][0]['items'][0])
        self.assertNotIn('image_url',trip['daily_plan'][0]['items'][0])

    async def test_generic_strolls_do_not_consume_matching_allowance_and_duplicates_reuse_photos(self):
        trip={'daily_plan':[{'destination':'Rome','items':[
            {'card_type':'activity','title':'Optional short Tiber stroll','location':'Rome'},
            {'card_type':'activity','title':'Historic-center orientation','location':'Rome'},
            {'card_type':'activity','title':'Colosseum','location':'Rome'},
            {'card_type':'activity','title':'Colosseum','location':'Rome'},
            {'card_type':'activity','title':'Roman Forum','location':'Rome'}]}]}
        with search_context(providers={'places':'brave'}),patch.dict('os.environ',{'BRAVE_ITINERARY_PLACE_LIMIT':'1'}),patch('trvelle.tools.place_search.find_place',new=AsyncMock(return_value=normalize_place(candidate(name='Colosseum',city='Rome'),1))) as find:
            result=await enrich_itinerary(trip)
        find.assert_awaited_once()
        items=result['daily_plan'][0]['items']
        self.assertEqual(items[2]['image_url'],items[3]['image_url'])
        self.assertNotIn('image_url',items[0])
        self.assertNotIn('image_url',items[4])

    async def test_brave_failure_uses_tavily_but_comparisons_never_hide_provider_failures(self):
        fallback=AsyncMock(return_value={'raw':{'results':[{'title':'Uffizi','url':'https://www.uffizi.it/','content':'Timed tickets'}]}})
        fake_tool=type('Tavily',(),{'ainvoke':fallback})()
        with patch.dict('os.environ',{'TAVILY_API_KEY':'test'}),patch('trvelle.tools.web_search.gateway.arequest',new=AsyncMock(side_effect=SearchBudgetError('Brave unavailable'))),patch('trvelle.tools.web_search.tavily_search',new=fake_tool):
            result=await research_search('Uffizi Florence tickets',provider='brave')
            self.assertEqual(result['provider'],'tavily')
            self.assertEqual(result['fallback_from'],'brave')
            with self.assertRaises(SearchBudgetError):await research_search('Uffizi Florence tickets',provider='brave',fallback=False)
        fallback.assert_awaited_once()

if __name__=='__main__':unittest.main()
