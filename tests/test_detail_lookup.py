import unittest
import json
from unittest.mock import AsyncMock, patch
from langchain_core.messages import AIMessage
from trvelle.tools.detail_lookup import DetailLookup, activity_query, merge_missing, missing_details


def option(number='LH 755', price=500):
    return {'price':price, 'flights':[{'flight_number':number, 'airline':'Lufthansa',
        'departure_airport':{'id':'BLR','time':'2026-12-10 02:50'},
        'arrival_airport':{'id':'MUC','time':'2026-12-10 08:10'},
        'extensions':['Wi-Fi']}], 'total_duration':560}


class DetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_summary_uses_owner_researcher_and_restores_request_context(self):
        import uuid
        from trvelle.orchestrator.personal_models import active_owner
        owner=uuid.uuid4();caller=uuid.uuid4()
        async def answer(role,tools,messages):
            self.assertEqual(active_owner.get(),str(owner))
            self.assertEqual(role,'researcher')
            return AIMessage(content='Scoped summary')
        router=type('Router',(),{'invoke':AsyncMock(side_effect=answer)})()
        lookup=DetailLookup(router,owner=owner)
        token=active_owner.set(str(caller))
        try:
            self.assertEqual((await lookup.summarize([])).content,'Scoped summary')
            self.assertEqual(active_owner.get(),str(caller))
            router.invoke.side_effect=RuntimeError('Fixture failure')
            with self.assertRaises(RuntimeError):await lookup.summarize([])
            self.assertEqual(active_owner.get(),str(caller))
        finally:active_owner.reset(token)

    async def test_baggage_rejects_destination_packages_and_other_airlines(self):
        lookup=DetailLookup();lookup.serp=AsyncMock(return_value={'organic_results':[
            {'title':'Japan Tokyo Osaka Kyoto tour','link':'https://tour.example/japan','snippet':'Posjeta Kabukiza teataru. 2 bags included.'},
            {'title':'Japan Airlines baggage','link':'https://cabinz.example/jal','snippet':'Two 23 kg bags.'},
            {'title':'Vietjet baggage','link':'https://vietjetair.com/en/baggage','snippet':'Checked baggage depends on fare.'},
            {'title':'JAL checked baggage','link':'https://www.jal.co.jp/jp/en/inter/baggage/checked/','snippet':'Checked baggage allowance depends on cabin class.'}]})
        with patch.dict('os.environ',{'TAVILY_API_KEY':''}):
            report=await lookup.research('Japan Airlines baggage',{'kind':'flight','official_domains':['jal.co.jp']})
        self.assertEqual(report['summary'],'')
        self.assertEqual(report['status'],'sources_only')
        self.assertEqual([s['url'] for s in report['sources']],['https://www.jal.co.jp/jp/en/inter/baggage/checked/'])

    async def test_baggage_policy_is_summarized_in_english_without_verifying_fare(self):
        source='https://www.jal.co.jp/jp/en/inter/baggage/checked/'
        router=type('Router',(),{'invoke':AsyncMock(return_value=AIMessage(content=json.dumps({'summary':'General checked baggage allowance depends on cabin class. Confirm the selected fare and operating carrier.','source_urls':[source]})))})()
        lookup=DetailLookup(router)
        response={'results':[{'title':'JAL baggage','url':source,'content':'Checked baggage allowance for economy and business cabins.'},
                             {'title':'Japan holiday','url':'https://tour.example/japan','content':'Posjeta Kabukiza teataru.'}]}
        original=option('JL 30');original['flights'][0].update(airline='Japan Airlines',travel_class='Economy')
        with patch('trvelle.tools.web_search.research_search',new=AsyncMock(return_value=response)) as search:
            result,report=await lookup.fetch('flight',original,{'search_parameters':{'outbound_date':'2027-04-20'}},fields=['Baggage allowance'],web_provider='brave',context='Japan Tokyo Kyoto 2027')
        self.assertEqual(result['price'],original['price'])
        self.assertIn('Baggage allowance',report['missing'])
        self.assertFalse(result['baggage_policies'][0]['fare_verified'])
        self.assertIn('General checked baggage',result['baggage_policies'][0]['summary'])
        self.assertEqual(report['baggage_policy_version'],1)
        self.assertEqual(search.call_args.kwargs['include_domains'],['jal.co.jp'])
        self.assertEqual(search.call_args.kwargs['language'],'en')
        self.assertNotIn('Kyoto',search.call_args.args[0])
        self.assertNotIn('Kabukiza',router.invoke.call_args.args[2][1].content)
        self.assertIn('English',router.invoke.call_args.args[2][0].content)

    async def test_failed_baggage_summarizer_never_displays_raw_html_or_foreign_snippets(self):
        router=type('Router',(),{'invoke':AsyncMock(side_effect=RuntimeError('Model unavailable'))})()
        lookup=DetailLookup(router);lookup.serp=AsyncMock(return_value={'organic_results':[
            {'title':'JAL baggage','link':'https://www.jal.co.jp/jp/en/inter/baggage/checked/','snippet':'Checked baggage: <strong>23 kg</strong> &amp; special conditions.'}]})
        with patch.dict('os.environ',{'TAVILY_API_KEY':''}):
            report=await lookup.research('JAL baggage',{'kind':'flight','official_domains':['jal.co.jp']})
        self.assertEqual(report['summary'],'')
        self.assertEqual(report['status'],'sources_only')
        self.assertEqual(len(report['sources']),1)

    async def test_missing_ticket_price_uses_marked_model_range_when_search_has_no_price(self):
        payload = {'summary': '', 'cost': {'min_price': 800, 'max_price': 1600, 'currency': 'INR', 'scope': 'per_person', 'status': 'estimate', 'basis': 'Typical museum admission from model knowledge; unverified.', 'source_url': 'https://invented.example'}}
        router = type('Router', (), {'invoke': AsyncMock(return_value=AIMessage(content=json.dumps(payload)))})()
        lookup = DetailLookup(router)
        lookup.serp = AsyncMock(return_value={'organic_results': []})
        with patch.dict('os.environ', {'TAVILY_API_KEY': ''}):
            item, report = await lookup.fetch('activity', {'title': 'Museum', 'location': 'Tokyo'}, currency='INR', fields=['Ticket price'])
        self.assertEqual(item['cost']['price'], 1600)
        self.assertEqual(item['cost']['status'], 'estimate')
        self.assertNotIn('source_url', item['cost'])
        self.assertNotIn('visitor_information', item)
        self.assertEqual(report['missing'], [])
        self.assertEqual(report['filled'], ['Ticket price'])
        self.assertEqual(report['status'], 'estimated')

    async def test_unsourced_model_quote_is_not_saved_as_a_verified_price(self):
        payload = {'cost': {'price': 1000, 'currency': 'INR', 'scope': 'per_person', 'status': 'quoted', 'source_url': 'https://invented.example'}}
        router = type('Router', (), {'invoke': AsyncMock(return_value=AIMessage(content=json.dumps(payload)))})()
        lookup = DetailLookup(router)
        lookup.serp = AsyncMock(return_value={'organic_results': []})
        with patch.dict('os.environ', {'TAVILY_API_KEY': ''}):
            item, report = await lookup.fetch('activity', {'title': 'Museum', 'location': 'Tokyo'}, currency='INR', fields=['Ticket price'])
        self.assertNotIn('cost', item)
        self.assertEqual(report['missing'], ['Ticket price'])

    async def test_scoped_baggage_lookup_does_not_research_unrelated_fields(self):
        original=option();original.pop('total_duration')
        lookup=DetailLookup();lookup.serp=AsyncMock(return_value={'best_flights':[]})
        lookup.research=AsyncMock(return_value={'summary':'The fare-specific allowance was not verified.','sources':[],'status':'unavailable'})
        result,report=await lookup.fetch('flight',original,{'search_parameters':{'outbound_date':'2026-12-10'}},'INR',fields=['Baggage allowance'])
        self.assertEqual(result,original)
        self.assertEqual(report['fields'],['Baggage allowance'])
        self.assertEqual(report['missing'],['Baggage allowance'])
        self.assertEqual(lookup.research.call_args.args[1]['missing'],['Baggage allowance'])
        self.assertNotIn('Duration',lookup.research.call_args.args[0])

    async def test_available_requested_field_skips_provider_and_model_calls(self):
        lookup=DetailLookup();lookup.serp=AsyncMock();lookup.research=AsyncMock()
        original={'name':'Hotel','room_description':'Private double room'}
        result,report=await lookup.fetch('hotel',original,fields=['Room configuration'])
        self.assertEqual(result,original);self.assertEqual(report['missing'],[])
        lookup.serp.assert_not_awaited();lookup.research.assert_not_awaited()

    async def test_ticket_lookup_keeps_summary_beside_price_without_claiming_a_verified_quote(self):
        lookup=DetailLookup();lookup.research=AsyncMock(return_value={'summary':'Adult tickets start at EUR 25; future-date prices are unverified.',
            'sources':[{'url':'https://museum.example/tickets','title':'Tickets'}],'status':'researched'})
        item={'title':'Museum','location':'Rome','description':'Visit the museum'}
        result,report=await lookup.fetch('activity',item,fields=['Ticket price'])
        self.assertNotIn('visitor_information',result)
        self.assertNotIn('cost',result)
        self.assertEqual(report['missing'],['Ticket price'])
        self.assertEqual(report['filled'],[])

    async def test_exact_flight_match_fills_holes_without_changing_fare_or_selection(self):
        original = option()
        fresh = option(price=900)
        fresh['flights'][0].update(airline_logo='https://airline.example/logo.png', extensions=['Checked baggage: 23 kg'])
        lookup = DetailLookup()
        lookup.serp = AsyncMock(return_value={'best_flights':[option('LH 999'),fresh]})
        lookup.research = AsyncMock()
        result, report = await lookup.fetch('flight',original,{'search_parameters':{'departure_id':'BLR','arrival_id':'MUC','outbound_date':'2026-12-10','type':2}},'INR')
        self.assertEqual(result['price'],500)
        self.assertEqual(result['flights'][0]['flight_number'],'LH 755')
        self.assertIn('Checked baggage: 23 kg',result['flights'][0]['extensions'])
        self.assertEqual(original['flights'][0]['extensions'],['Wi-Fi'])
        self.assertEqual(report['missing'],[])
        lookup.research.assert_not_awaited()
        self.assertEqual(lookup.serp.call_args.args[0]['currency'],'INR')

    async def test_recovered_fare_uses_the_requested_currency(self):
        original=option()
        original.pop('price')
        original['currency']='USD'
        lookup=DetailLookup()
        lookup.serp=AsyncMock(return_value={'best_flights':[option(price=900)]})
        lookup.research=AsyncMock(return_value={'summary':'','sources':[]})
        result,report=await lookup.fetch('flight',original,{'search_parameters':{'outbound_date':'2026-12-10','currency':'USD'}},'INR')
        self.assertEqual(result['price'],900)
        self.assertEqual(result['currency'],'INR')

    async def test_changed_flight_identity_is_not_overwritten_by_first_search_result(self):
        original = option()
        lookup = DetailLookup()
        lookup.serp = AsyncMock(return_value={'best_flights':[option('LH 999')]})
        lookup.research = AsyncMock(return_value={'summary':'No verified baggage allowance.','sources':[]})
        result, report = await lookup.fetch('flight',original,{'search_parameters':{'outbound_date':'2026-12-10'}})
        self.assertEqual(result,original)
        self.assertIn('Baggage allowance',report['missing'])

    async def test_hotel_token_lookup_preserves_existing_price_and_populates_actual_images(self):
        hotel = {'name':'Hotel One','property_token':'token','rate_per_night':{'lowest':'₹1000'}}
        lookup = DetailLookup()
        lookup.serp = AsyncMock(return_value={'name':'Hotel One','property_token':'token','description':'By the river','amenities':['Wi-Fi'],
            'images':[{'original_image':'https://hotel.example/image.jpg'}], 'check_in_time':'15:00','check_out_time':'11:00',
            'nearby_places':[{'name':'Station'}],'rate_per_night':{'lowest':'₹2000'},'total_rate':{'lowest':'₹4000'}})
        lookup.research = AsyncMock()
        result, report = await lookup.fetch('hotel',hotel,{'search_parameters':{'q':'Venice','check_in_date':'2026-12-10','check_out_date':'2026-12-12'}},'INR')
        self.assertEqual(result['rate_per_night']['lowest'],'₹1000')
        self.assertEqual(result['images'][0]['original_image'],'https://hotel.example/image.jpg')
        self.assertEqual(lookup.serp.call_args.args[0]['property_token'],'token')
        self.assertEqual(report['missing'],[])

    async def test_lookup_failure_preserves_item_and_uses_sourced_fallback(self):
        lookup = DetailLookup()
        lookup.serp = AsyncMock(side_effect=RuntimeError('Quota exhausted'))
        lookup.research = AsyncMock(return_value={'summary':'Check the hotel website.','sources':[{'title':'Hotel','url':'https://hotel.example'}]})
        hotel={'name':'Hotel One','property_token':'token'}
        result, report = await lookup.fetch('hotel',hotel,{'search_parameters':{'q':'Venice','check_in_date':'2026-12-10','check_out_date':'2026-12-12'}})
        self.assertEqual(result,hotel)
        self.assertIn('provider_notice',report)
        self.assertEqual(report['sources'][0]['url'],'https://hotel.example')

    async def test_booking_lookup_keeps_provider_offers_and_selected_hotel_quote(self):
        hotel = {'name':'Hotel One', 'property_token':'token', 'rate_per_night':{'lowest':'₹1000'}}
        offer = {'source':'Booking.com', 'link':'https://booking.example/hotel?checkin=2027-03-14',
                 'rate_per_night':{'lowest':'₹1200'}, 'total_rate':{'lowest':'₹3600'}, 'free_cancellation':True}
        lookup = DetailLookup()
        lookup.serp = AsyncMock(return_value={'name':'Hotel One', 'property_token':'token',
            'rate_per_night':{'lowest':'₹1200'}, 'prices':[offer], 'featured_prices':[offer]})
        lookup.research = AsyncMock()
        result, report = await lookup.fetch('hotel', hotel, {'search_parameters':{'q':'Rome',
            'check_in_date':'2027-03-14', 'check_out_date':'2027-03-17', 'adults':2}}, 'INR', booking_only=True)
        self.assertEqual(result['prices'],[offer])
        self.assertEqual(result['featured_prices'],[offer])
        self.assertEqual(result['rate_per_night']['lowest'],'₹1000')
        self.assertEqual(report['filled'],['Booking options'])
        self.assertEqual(report['missing'],[])
        self.assertEqual(lookup.serp.call_args.args[0]['adults'],2)
        lookup.serp.assert_awaited_once()
        lookup.research.assert_not_awaited()
        self.assertNotIn('prices',hotel)

    async def test_saved_booking_offers_skip_provider_requests(self):
        hotel = {'name':'Hotel One', 'prices':[{'source':'Booking.com', 'link':'https://booking.example/hotel'}]}
        lookup = DetailLookup()
        lookup.serp, lookup.research = AsyncMock(), AsyncMock()
        result, report = await lookup.fetch('hotel', hotel, booking_only=True)
        self.assertEqual(result,hotel)
        self.assertEqual(report['status'],'complete')
        lookup.serp.assert_not_awaited()
        lookup.research.assert_not_awaited()

    async def test_booking_provider_failure_does_not_spend_fallback_credits(self):
        lookup = DetailLookup()
        lookup.serp = AsyncMock(side_effect=RuntimeError('Quota exhausted'))
        lookup.research = AsyncMock()
        result, report = await lookup.fetch('hotel', {'name':'Hotel One', 'property_token':'token'},
            {'search_parameters':{'q':'Rome','check_in_date':'2027-03-14','check_out_date':'2027-03-17'}}, booking_only=True)
        self.assertEqual(report['status'],'unavailable')
        self.assertIn('provider_notice',report)
        self.assertNotIn('prices',result)
        lookup.research.assert_not_awaited()

    async def test_booking_lookup_rejects_other_properties_and_unsafe_links(self):
        lookup = DetailLookup()
        lookup.serp = AsyncMock(return_value={'name':'Unrelated hotel', 'property_token':'different',
            'prices':[{'source':'Wrong hotel','link':'https://booking.example/other'}]})
        lookup.research = AsyncMock()
        original = {'name':'Hotel One', 'property_token':'token', 'prices':[{'link':'javascript:alert(1)'}]}
        result, report = await lookup.fetch('hotel', original,
            {'search_parameters':{'q':'Rome','check_in_date':'2027-03-14','check_out_date':'2027-03-17'}}, booking_only=True)
        self.assertEqual(result,original)
        self.assertEqual(report['status'],'unavailable')
        lookup.research.assert_not_awaited()

    async def test_model_cooldown_saves_web_evidence_without_changing_activity_identity(self):
        router = type('Router',(),{'invoke':AsyncMock(side_effect=RuntimeError('All models cooling down'))})()
        lookup=DetailLookup(router)
        lookup.serp=AsyncMock(return_value={'organic_results':[{'title':'Museum','link':'https://museum.example','snippet':'Open Tuesday to Sunday.'}]})
        original={'title':'Museum visit','location':'Venice'}
        result, report=await lookup.fetch('activity',original,context='Venice')
        self.assertEqual({key:result[key] for key in original},original)
        self.assertIn('Open Tuesday', result['visitor_information'])
        self.assertEqual(result['visitor_information_sources'], report['sources'])
        self.assertIn('Open Tuesday',report['summary'])
        self.assertEqual(report['method'],'web')
        router.invoke.assert_awaited_once()

    def test_visitor_information_is_optional_for_an_ordinary_walk(self):
        self.assertEqual(missing_details('activity', {'title':'River walk', 'description':'Walk by the river.', 'location':'Florence'}), [])

    def test_sourced_visit_notes_do_not_trigger_a_redundant_missing_description(self):
        self.assertEqual(missing_details('activity', {'title':'Museum', 'visitor_information':'Reserve timed admission.', 'location':'Rome'}), [])

    def test_lookup_query_targets_the_named_attraction_and_known_source(self):
        query = activity_query({'title':'Colosseum', 'location':'Piazza del Colosseo, Rome', 'source_url':'https://colosseo.it/en/'})
        self.assertIn('Colosseum', query)
        self.assertIn('site:colosseo.it', query)
        self.assertIn('(opening hours OR tickets OR booking OR accessibility)', query)
        self.assertNotIn('Visitor information', query)

    async def test_optional_lookup_runs_and_persists_sourced_visit_notes(self):
        original={'title':'Colosseum', 'description':'Visit the amphitheatre.', 'location':'Rome', 'source_url':'https://colosseo.it/en/'}
        lookup=DetailLookup()
        lookup.serp=AsyncMock(return_value={'organic_results':[
            {'title':'Rome TV show', 'link':'https://imdb.com/rome', 'snippet':'Open the TV guide.'},
            {'title':'Colosseum opening times', 'link':'https://colosseo.it/en/opening-times/', 'snippet':'Timed tickets must be booked in advance.'}]})
        result,report=await lookup.fetch('activity',original)
        self.assertIn('Timed tickets',result['visitor_information'])
        self.assertEqual(result['source_url'],original['source_url'])
        self.assertEqual(len(report['sources']),1)
        self.assertEqual(report['sources'][0]['url'],'https://colosseo.it/en/opening-times/')
        self.assertEqual(report['missing'],[])
        self.assertEqual(report['filled'],['Visit details'])
        self.assertNotIn('visitor_information', original)

    async def test_legacy_visit_notes_are_retained_on_refresh(self):
        original={'title':'Museum', 'description':'Visit the gallery.', 'location':'Venice', 'visitor_details':{'booking':'Timed entry recommended'}}
        lookup=DetailLookup()
        lookup.research=AsyncMock(return_value={'status':'researched', 'summary':'Open Tuesday.', 'sources':[{'url':'https://museum.example','title':'Museum'}]})
        result,report=await lookup.fetch('activity', original)
        self.assertEqual(result['visitor_information'],original['visitor_details'])
        self.assertEqual(report['missing'],[])
        self.assertEqual(report['filled'],[])
        self.assertIn('Open Tuesday', report['summary'])

    async def test_unrelated_results_cannot_fill_visit_information(self):
        original={'title':'Colosseum', 'description':'Visit the amphitheatre.', 'location':'Rome', 'source_url':'https://colosseo.it/en/'}
        lookup=DetailLookup()
        lookup.serp=AsyncMock(return_value={'organic_results':[{'title':'Rome TV show', 'link':'https://imdb.com/rome', 'snippet':'Open the TV guide.'}]})
        with patch.dict('os.environ', {'TAVILY_API_KEY':''}):
            result,report=await lookup.fetch('activity', original)
        self.assertEqual(result,original)
        self.assertEqual(report['status'],'unavailable')
        self.assertEqual(report['sources'],[])
        self.assertEqual(report['missing'],[])

    async def test_failed_optional_lookup_uses_targeted_tavily_fallback(self):
        lookup=DetailLookup()
        lookup.serp=AsyncMock(return_value={'organic_results':[]})
        response={'result':'{"results":[{"title":"Uffizi tickets","url":"https://www.uffizi.it/en/tickets","content":"Book admission tickets in advance."}]}'}
        fallback=type('Tavily',(),{'ainvoke':AsyncMock(return_value=response)})()
        with patch.dict('os.environ', {'TAVILY_API_KEY':'test'}), patch('trvelle.tools.web_search.tavily_search', new=fallback):
            result,report=await lookup.fetch('activity', {'title':'Uffizi Gallery','description':'View art.','location':'Florence','source_url':'https://www.uffizi.it/en/the-uffizi'})
        self.assertIn('admission tickets',result['visitor_information'])
        self.assertIn('site:uffizi.it',fallback.ainvoke.call_args.args[0]['query'])
        self.assertEqual(fallback.ainvoke.call_args.args[0]['include_domains'],['uffizi.it'])
        self.assertEqual(report['status'],'researched')

    async def test_domain_filter_is_forwarded_by_the_tavily_tool(self):
        from trvelle.tools.web_search import tavily_search
        with patch('trvelle.tools.web_search.gateway.arequest', new=AsyncMock(return_value={'results':[]})) as request:
            await tavily_search.ainvoke({'query':'Uffizi visit details','include_domains':['www.uffizi.it','uffizi.it']})
        self.assertEqual(request.call_args.args[1]['include_domains'],['uffizi.it'])
        self.assertEqual(request.call_args.args[1]['search_depth'],'basic')

    async def test_provider_failure_is_reported_separately_from_missing_information(self):
        from trvelle.tools.search_gateway import SearchBudgetError
        lookup=DetailLookup()
        lookup.serp=AsyncMock(side_effect=SearchBudgetError('Search budget is exhausted.'))
        with patch.dict('os.environ', {'TAVILY_API_KEY':''}):
            report=await lookup.research('Museum hours',{'kind':'activity','name':'Museum'})
        self.assertEqual(report['status'],'unavailable')
        self.assertIn('budget is exhausted',report['provider_notice'])

    async def test_ai_is_only_given_search_evidence_and_keeps_sources(self):
        router=type('Router',(),{'invoke':AsyncMock(return_value=AIMessage(content='The museum closes on Mondays.'))})()
        lookup=DetailLookup(router)
        lookup.serp=AsyncMock(return_value={'organic_results':[{'title':'Museum','link':'https://museum.example','snippet':'Closed on Mondays.'}]})
        result=await lookup.research('Museum opening hours',{'name':'Museum'})
        self.assertEqual(result['method'],'ai')
        self.assertEqual(result['sources'][0]['url'],'https://museum.example')
        self.assertIn('Closed on Mondays',router.invoke.call_args.args[2][1].content)

    def test_merge_preserves_zero_and_existing_nested_values(self):
        self.assertEqual(merge_missing({'price':0,'airport':{'id':'BLR'}},{'price':500,'airport':{'id':'DEL','name':'Bangalore'}}),
                         {'price':0,'airport':{'id':'BLR','name':'Bangalore'}})

if __name__=='__main__':unittest.main()
