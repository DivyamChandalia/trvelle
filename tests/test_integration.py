"""Integration checks against the configured local PostgreSQL database."""
import asyncio
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from trvelle.orchestrator.api_wrapper import app, db_handler
from trvelle.database.models import User, ChatSession, Message, ToolExecution
from trvelle.tools.itinerary_tool import itinerary_tool, Itinerary

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user_a, self.user_b, self.chat_a, self.chat_b, self.itinerary_id = [uuid.uuid4() for _ in range(5)]
        self.hotel_uid = 'hotel-' + str(uuid.uuid4())
        self.raw = {'trip_name': 'Integration itinerary', 'daily_plan': []}
        with db_handler.db_session() as db:
            db.add_all([User(user_id=self.user_a), User(user_id=self.user_b)])
            db.flush()
            db.add_all([ChatSession(chat_id=self.chat_a, user_id=self.user_a), ChatSession(chat_id=self.chat_b, user_id=self.user_b)])
            db.flush()
            db.add(ToolExecution(message_id=self.itinerary_id, chat_id=self.chat_a, tool_name='itinerary_tool', raw_response=self.raw))
            db.add(Message(message_id=self.itinerary_id, chat_id=self.chat_a, user_id=self.user_a, type='tool', content='Itinerary', message_name='itinerary_tool'))
            db.add(ToolExecution(message_id=uuid.uuid4(), chat_id=self.chat_a, tool_name='hotel_search', created_at=datetime.now(timezone.utc) - timedelta(minutes=1), raw_response={'properties': [{'name': 'Selected hotel', 'choose_uid': self.hotel_uid, 'images': [{'original_image': 'https://example.com/hotel.jpg'}]}]}))
            db.commit()
        self.headers = {'x-backend-token': os.environ['BACKEND_API_TOKEN'], 'user-id': str(self.user_a)}

    def tearDown(self):
        with db_handler.db_session() as db:
            for identifier in (self.user_a, self.user_b):
                db.delete(db.get(User, identifier))
            db.commit()
        self.client.close()

    def test_budget_allocation_persists_versions_and_rejects_wrong_owner_or_stale_edits(self):
        body = {'itinerary_id': str(self.itinerary_id), 'revision': 1, 'currency': 'INR',
                'activities': 1500, 'meals': 2500, 'transport': 500, 'buffer': 300}
        params = {'chat_id': str(self.chat_a)}
        self.assertEqual(self.client.post('/budget_allocation', params=params,
            headers={**self.headers, 'user-id': str(self.user_b)}, json=body).status_code, 404)
        response = self.client.post('/budget_allocation', params=params, headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['itinerary']['budget_allocations']['meals'], 2500)
        self.assertEqual(response.json()['itinerary']['revision'], 2)
        self.assertEqual(db_handler.get_itinerary(self.user_a, self.chat_a, self.itinerary_id)['budget_allocations']['currency'], 'INR')
        self.assertNotIn('budget_allocations', db_handler.get_itinerary(self.user_a, self.chat_a, self.itinerary_id, 1))
        self.assertEqual(self.client.post('/budget_allocation', params=params, headers=self.headers, json=body).status_code, 409)
        self.assertEqual(self.client.post('/budget_allocation', params=params, headers=self.headers,
            json={**body, 'revision': 2, 'meals': -1}).status_code, 422)

    def test_chat_history_keeps_answers_and_plan_summary_but_omits_tool_commentary(self):
        with db_handler.db_session() as db:
            db.add_all([Message(message_id=uuid.uuid4(), chat_id=self.chat_a, user_id=self.user_a, type='ai', message_name='Supervisor_Agent',
                content='I will check another flight before choosing the stay dates.', additional_kwargs={'tool_calls':[{'name':'flight_search','args':{},'id':'test-flight','type':'tool_call'}]}),
                Message(message_id=uuid.uuid4(), chat_id=self.chat_a, user_id=self.user_a, type='ai', message_name='Supervisor_Agent', content='What dates do you prefer?')])
            db.commit()
        messages = db_handler.get_filtered_chat_history(self.user_a, self.chat_a)
        self.assertNotIn('I will check another flight before choosing the stay dates.', [row['content'] for row in messages])
        self.assertIn('What dates do you prefer?', [row['content'] for row in messages])
        plan = next(row for row in messages if row.get('metadata', {}).get('itineraryId'))
        self.assertEqual(plan['content'], 'Your itinerary is saved.')
        self.assertEqual(len(db_handler.load_chat_history(self.user_a, self.chat_a)), 3)  # Internal evidence is preserved.

    def test_hotel_booking_offers_are_saved_without_starting_a_model_and_keep_currency(self):
        with db_handler.db_session() as db:
            search = db.query(ToolExecution).filter_by(chat_id=self.chat_a, tool_name='hotel_search').one()
            search.raw_response = {'search_parameters':{'q':'Rome','check_in_date':'2027-03-14',
                'check_out_date':'2027-03-17','adults':2,'currency':'INR'}, 'properties':[
                {'name':'Selected hotel','choose_uid':self.hotel_uid,'property_token':'token',
                 'rate_per_night':{'lowest':'₹1000','extracted_lowest':1000}}]}
            db.commit()
        offer = {'source':'Booking.com','link':'https://booking.example/hotel',
                 'rate_per_night':{'lowest':'₹1200','extracted_lowest':1200},
                 'total_rate':{'lowest':'₹3600','extracted_lowest':3600}}
        body = {'itinerary_id':str(self.itinerary_id),'kind':'hotel','uid':self.hotel_uid,
                'purpose':'booking','revision':1,'currency':'INR'}
        with patch('trvelle.orchestrator.api_wrapper.get_orchestrator') as model, patch(
            'trvelle.tools.detail_lookup.DetailLookup.serp', new=AsyncMock(return_value={
                'name':'Selected hotel','property_token':'token','prices':[offer]})) as search:
            denied = self.client.post('/fetch_details', params={'chat_id':str(self.chat_a)},
                headers={**self.headers,'user-id':str(self.user_b)}, json=body)
            self.assertEqual(denied.status_code,404)
            response = self.client.post('/fetch_details', params={'chat_id':str(self.chat_a)}, headers=self.headers, json=body)
            self.assertEqual(response.status_code,200,response.text)
            itinerary = response.json()['itinerary']
            saved = itinerary['hotel_details'][self.hotel_uid]
            self.assertEqual(saved['prices'][0]['total_rate']['extracted_lowest'],3600)
            self.assertEqual(saved['prices'][0]['total_rate']['currency'],'INR')
            self.assertEqual(saved['rate_per_night']['extracted_lowest'],1000)
            self.assertEqual(itinerary['detail_reports'][f'hotel-booking:{self.hotel_uid}']['status'],'complete')
            self.assertEqual(itinerary['revision'],2)
            model.assert_not_called()
            search.assert_awaited_once()
            self.assertEqual(self.client.post('/fetch_details', params={'chat_id':str(self.chat_a)}, headers=self.headers,
                json=body).status_code,409)
            self.assertEqual(self.client.post('/fetch_details', params={'chat_id':str(self.chat_a)}, headers=self.headers,
                json={**body,'kind':'flight','revision':2}).status_code,404)
            self.assertEqual(self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,
                json={**body,'kind':'activity','revision':2}).status_code,422)

    def test_optional_brave_place_lookup_persists_structured_details_and_photos_without_model_calls(self):
        candidate = {'id':'temporary', 'title':'Uffizi Gallery', 'url':'https://www.uffizi.it/',
            'postal_address':{'displayAddress':'Florence, IT','addressLocality':'Florence','country':'IT'},
            'rating':{'ratingValue':4.7,'bestRating':5,'reviewCount':4000},
            'thumbnail':{'src':'https://example.test/uffizi.jpg'}}
        with db_handler.db_session() as db:
            execution=db.get(ToolExecution,self.itinerary_id)
            execution.raw_response={'trip_name':'Florence trip','daily_plan':[{'day':1,'destination':'Florence',
                'items':[{'card_type':'activity','title':'Uffizi Gallery','location':'Florence Italy','source_url':'https://www.uffizi.it/en/'}]}]}
            db.commit()
        body={'itinerary_id':str(self.itinerary_id),'revision':1,'kind':'activity','day_index':0,'item_index':0,
              'purpose':'place','fetch_photos':True,'currency':'INR'}
        with patch('trvelle.orchestrator.api_wrapper.get_orchestrator') as model,patch(
            'trvelle.tools.place_search.gateway.arequest',new=AsyncMock(return_value={'results':[candidate]})) as request:
            denied=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers={**self.headers,'user-id':str(self.user_b)},json=body)
            self.assertEqual(denied.status_code,404)
            response=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body)
            self.assertEqual(response.status_code,200,response.text)
            item=response.json()['itinerary']['daily_plan'][0]['items'][0]
            self.assertEqual(item['image_url'],'https://example.test/uffizi.jpg')
            self.assertEqual(item['place_details']['provider'],'brave')
            self.assertNotIn('id',item['place_details'])
            self.assertEqual(item['place_details']['rating'],4.7)
            model.assert_not_called();request.assert_awaited_once()

    def test_search_provider_defaults_are_server_selected_and_saved_for_resume(self):
        with patch.dict('os.environ',{'TRVELLE_WEB_SEARCH_PROVIDER':'brave','TRVELLE_PLACE_SEARCH_PROVIDER':'brave','BRAVE_RUN_REQUEST_LIMIT':'20','BRAVE_ITINERARY_PLACE_LIMIT':'8'}):
            response=self.client.post('/runs/start',params={'chat_id':str(self.chat_a)},headers=self.headers,
                json={'message':'Plan Florence','web_provider':'tavily','place_provider':'disabled'})
        self.assertEqual(response.status_code,200,response.text)
        run=response.json()
        self.assertEqual(run['search_providers'],{'web':'brave','places':'brave'})
        self.assertIn('brave',run['search_limits'])
        self.assertEqual(run['search_usage']['brave'],0)
        from trvelle.database.models import PlanningRun
        from trvelle.orchestrator.api_wrapper import ChatRequest,DetailRequest
        with db_handler.db_session() as db:
            saved=db.get(PlanningRun,uuid.UUID(run['run_id']))
            self.assertEqual(saved.request['search_providers'],{'web':'brave','places':'brave'})
            self.assertEqual(saved.request['search_reserves'],{'brave_places':8})
            self.assertEqual(saved.request['search_limits']['brave'],20)
        for schema in (ChatRequest,DetailRequest):
            self.assertNotIn('web_provider',schema.model_fields)
            self.assertNotIn('place_provider',schema.model_fields)

    def test_manual_activity_research_uses_current_backend_defaults_for_old_plans(self):
        activity={'card_type':'activity','title':'Uffizi Gallery','location':'Florence Italy'}
        with db_handler.db_session() as db:
            execution=db.get(ToolExecution,self.itinerary_id)
            execution.raw_response={'trip_name':'Florence trip','search_providers':{'web':'tavily','places':'disabled'},
                'daily_plan':[{'day':1,'destination':'Florence','items':[activity]}]}
            db.commit()
        report={'summary':'','filled':[],'missing':[],'sources':[],'fetched_at':datetime.now(timezone.utc).isoformat()}
        with patch.dict('os.environ',{'TRVELLE_WEB_SEARCH_PROVIDER':'brave','TRVELLE_PLACE_SEARCH_PROVIDER':'brave'}),patch(
            'trvelle.orchestrator.api_wrapper.get_orchestrator'),patch('trvelle.tools.detail_lookup.DetailLookup.fetch',
            new=AsyncMock(return_value=(activity,report))) as lookup:
            response=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,
                json={'itinerary_id':str(self.itinerary_id),'revision':1,'kind':'activity','day_index':0,'item_index':0,
                      'currency':'INR','web_provider':'tavily','place_provider':'disabled'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(lookup.call_args.kwargs['web_provider'],'brave')

    def test_field_lookup_is_scoped_owned_and_saves_notes_under_its_field(self):
        activity={'card_type':'activity','title':'Uffizi Gallery','location':'Florence Italy'}
        with db_handler.db_session() as db:
            db.get(ToolExecution,self.itinerary_id).raw_response={'trip_name':'Florence','daily_plan':[{'day':1,'items':[activity]}]}
            db.commit()
        body={'itinerary_id':str(self.itinerary_id),'kind':'activity','day_index':0,'item_index':0,'revision':1,'fields':['Ticket price']}
        report={'summary':'No date-specific ticket quote.','sources':[],'missing':['Ticket price'],'filled':[]}
        with patch('trvelle.orchestrator.api_wrapper.get_orchestrator'),patch('trvelle.tools.detail_lookup.DetailLookup.fetch',new=AsyncMock(return_value=(activity,report))) as lookup:
            bad=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json={**body,'fields':['Baggage allowance']})
            self.assertEqual(bad.status_code,422);lookup.assert_not_awaited()
            denied=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers={**self.headers,'user-id':str(self.user_b)},json=body)
            self.assertEqual(denied.status_code,404)
            response=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(lookup.call_args.kwargs['fields'],['Ticket price'])
        saved=response.json()['itinerary']
        self.assertEqual(saved['detail_reports']['activity:0:0:Ticket price'],report)
        self.assertNotIn('activity:0:0',saved['detail_reports'])

    def test_rejected_hotel_mentioned_in_notes_is_not_automatically_selected(self):
        with db_handler.db_session() as db:
            db.get(ToolExecution,self.itinerary_id).raw_response={'trip_name':'Rome','summary':{'dates':{'start':'2027-03-19','end':'2027-03-20'}},
                'trip_description':'Selected hotel is unsuitable; do not choose Selected hotel.', 'daily_plan':[{'day':1,'items':[]},{'day':2,'items':[]}]}
            search=db.query(ToolExecution).filter_by(chat_id=self.chat_a,tool_name='hotel_search').one()
            search.raw_response={**search.raw_response,'search_parameters':{'q':'Rome','check_in_date':'2027-03-19','check_out_date':'2027-03-20'}}
            db.commit()
        plan=db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        self.assertFalse(plan['travel_options']['hotels'][0]['selected'])
        self.assertIsNone(plan['validation']['priced_total'])
        self.assertEqual(plan['planning_status'],'partial')
        self.assertEqual([issue['date'] for issue in plan['validation']['issues'] if issue['code']=='hotel_missing'],['2027-03-19'])

    def add_flight_options(self):
        uid = 'selection-' + str(uuid.uuid4())
        raw = [{'search_parameters': {'currency':'INR'}, 'best_flights':[{'price':100, 'flights':[{'airline':'First'}]}, {'price':200, 'flights':[{'airline':'Second'}]}]}, {'choose_uid':uid}]
        with db_handler.db_session() as db:
            db.add(ToolExecution(message_id=uuid.uuid4(), chat_id=self.chat_a, tool_name='flight_search', unique_identifier=str(self.itinerary_id), raw_response=raw))
            db.commit()
        return {'itinerary_id':str(self.itinerary_id), 'uid':uid, 'search_index':0, 'option_index':1, 'currency':'INR'}

    def test_flight_selection_persists_in_itinerary(self):
        body = self.add_flight_options()
        response = self.client.post('/select_flight', params={'chat_id':str(self.chat_a)}, headers=self.headers, json=body)
        self.assertEqual(response.status_code,200,response.text)
        reloaded = db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        self.assertEqual(reloaded['travel_options']['flights'][0]['legs'][0]['flights'][0]['airline'],'Second')
        self.assertEqual(reloaded['travel_options']['flights'][0]['legs'][0]['price'],200)

    def test_versions_preserve_previous_selection_and_reject_stale_detail_requests(self):
        body = self.add_flight_options()
        response = self.client.post('/select_flight', params={'chat_id':str(self.chat_a)}, headers=self.headers, json={**body, 'revision':1})
        self.assertEqual(response.status_code,200,response.text)
        archived = db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id,1)
        latest = db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        self.assertEqual(archived['revision'],1)
        self.assertEqual(archived['latest_revision'],2)
        self.assertEqual(archived['travel_options']['flights'][0]['legs'][0]['flights'][0]['airline'],'First')
        self.assertEqual(latest['travel_options']['flights'][0]['legs'][0]['flights'][0]['airline'],'Second')
        stale=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json={'itinerary_id':str(self.itinerary_id),'kind':'flight','uid':body['uid'],'revision':1})
        self.assertEqual(stale.status_code,409)

    def test_exchange_rates_are_frozen_per_revision(self):
        from trvelle.utils.currency import present_currency
        trip={'itinerary_id':str(self.itinerary_id),'revision':1,'price':100,'currency':'USD'}
        with patch('trvelle.utils.currency.exchange_rate', AsyncMock(return_value=(80,'2026-10-03'))) as first:
            self.assertEqual(asyncio.run(present_currency(trip,'INR'))['price'],8000)
        with patch('trvelle.utils.currency.exchange_rate', AsyncMock(return_value=(90,'2026-10-04'))) as changed:
            self.assertEqual(asyncio.run(present_currency(trip,'INR'))['price'],8000)
            changed.assert_not_called()
            self.assertEqual(asyncio.run(present_currency({**trip,'revision':2},'INR'))['price'],9000)
        first.assert_awaited_once()

    def test_saved_versions_keep_their_offer_sources_when_new_quotes_arrive(self):
        with db_handler.db_session() as db:
            hotel=db.query(ToolExecution).filter_by(chat_id=self.chat_a,tool_name='hotel_search').first()
            raw=dict(hotel.raw_response)
            raw['properties']=[{**raw['properties'][0],'total_rate':{'extracted_lowest':100}}]
            hotel.raw_response=raw
            db.commit()
        plan=db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        db_handler.save_itinerary_edit(self.user_a,self.chat_a,self.itinerary_id,plan)
        with db_handler.db_session() as db:
            db.add(ToolExecution(message_id=uuid.uuid4(),chat_id=self.chat_a,tool_name='hotel_search',unique_identifier=str(self.itinerary_id),raw_response={'properties':[{'choose_uid':self.hotel_uid,'name':'Selected hotel','total_rate':{'extracted_lowest':999}}]}))
            db.commit()
        for revision in (1,2):
            saved=db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id,revision)
            self.assertEqual(saved['travel_options']['hotels'][0]['total_rate']['extracted_lowest'],100)

    def test_flight_selection_rejects_forged_option_and_wrong_owner(self):
        body = self.add_flight_options()
        body['option_index'] = 20
        response = self.client.post('/select_flight', params={'chat_id':str(self.chat_a)}, headers=self.headers, json=body)
        self.assertEqual(response.status_code,422)
        body['option_index'] = 1
        response = self.client.post('/select_flight', params={'chat_id':str(self.chat_a)}, headers={**self.headers,'user-id':str(self.user_b)}, json=body)
        self.assertEqual(response.status_code,404)
        self.assertNotIn('flight_selections',db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id))

    def test_followup_chat_keeps_indian_trip_currency(self):
        with db_handler.db_session() as db:
            row = db.get(ToolExecution, self.itinerary_id)
            row.raw_response = {'trip_name':'Indian-origin trip','summary':{'origin':'Bangalore'},'daily_plan':[]}
            db.commit()
        response = self.client.post('/runs/start',params={'chat_id':str(self.chat_a)},headers=self.headers,json={'message':'Change the hotel','stream':False})
        self.assertEqual(response.status_code,200)
        from trvelle.database.models import PlanningRun
        with db_handler.db_session() as db:
            self.assertEqual(db.get(PlanningRun,uuid.UUID(response.json()['run_id'])).request['currency'],'INR')

    def prepare_editable_stay(self):
        alternative = 'hotel-alt-' + str(uuid.uuid4())
        with db_handler.db_session() as db:
            hotel_search = db.query(ToolExecution).filter_by(chat_id=self.chat_a,tool_name='hotel_search').first()
            hotel_search.raw_response = {'search_parameters':{'q':'Venice','check_in_date':'2026-12-10','check_out_date':'2026-12-11','currency':'INR'},'properties':[{'name':'Selected hotel','choose_uid':self.hotel_uid},{'name':'Alternative hotel','choose_uid':alternative,'nearby_places':[{'name':'Station','transportations':[{'type':'Walking','duration':'5 min'}]}]}]}
            db.get(ToolExecution,self.itinerary_id).raw_response = {'trip_name':'Edit test','summary':{'origin':'Bangalore'},'daily_plan':[{'day':1,'items':[{'item_type':'card','card_type':'hotel','title':'Selected hotel','uid':self.hotel_uid},{'item_type':'card','card_type':'activity','title':'Visit a museum','description':'Museum visit'}]}]}
            db.commit()
        trip = db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        return alternative,trip['travel_options']['hotels'][0]['stay_key']

    def test_hotel_selection_persists_and_keeps_nearby_places(self):
        uid,stay_key = self.prepare_editable_stay()
        response = self.client.post('/select_hotel',params={'chat_id':str(self.chat_a)},headers=self.headers,json={'itinerary_id':str(self.itinerary_id),'uid':uid,'stay_key':stay_key,'currency':'INR'})
        self.assertEqual(response.status_code,200,response.text)
        trip = db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        self.assertEqual(trip['daily_plan'][0]['items'][0]['uid'],uid)
        self.assertEqual(trip['daily_plan'][0]['items'][0]['title'],'Alternative hotel')
        chosen = next(h for h in trip['travel_options']['hotels'] if h['choose_uid']==uid)
        self.assertEqual(chosen['nearby_places'][0]['transportations'][0]['duration'],'5 min')

    def test_hotel_selection_rejects_wrong_stay_and_owner(self):
        uid,key = self.prepare_editable_stay()
        body={'itinerary_id':str(self.itinerary_id),'uid':uid,'stay_key':'wrong','currency':'INR'}
        self.assertEqual(self.client.post('/select_hotel',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body).status_code,422)
        body['stay_key']=key
        self.assertEqual(self.client.post('/select_hotel',params={'chat_id':str(self.chat_a)},headers={**self.headers,'user-id':str(self.user_b)},json=body).status_code,404)

    def test_activity_edit_persists_and_validates_time_and_owner(self):
        self.prepare_editable_stay()
        body={'itinerary_id':str(self.itinerary_id),'day_index':0,'item_index':1,'title':'Visit art gallery','description':'Explore paintings','location':'Gallery Venice','start_time':'10:30','end_time':'12:00','currency':'INR'}
        response=self.client.post('/edit_activity',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body)
        self.assertEqual(response.status_code,200,response.text)
        edited=db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)['daily_plan'][0]['items'][1]
        self.assertEqual(edited['title'],'Visit art gallery')
        self.assertEqual(edited['start_time'],'10:30')
        self.assertEqual(edited['location'],'Gallery Venice')
        self.assertEqual(self.client.post('/edit_activity',params={'chat_id':str(self.chat_a)},headers=self.headers,json={**body,'start_time':'25:00'}).status_code,422)
        self.assertEqual(self.client.post('/edit_activity',params={'chat_id':str(self.chat_a)},headers={**self.headers,'user-id':str(self.user_b)},json=body).status_code,404)
        self.assertEqual(self.client.post('/edit_activity',params={'chat_id':str(self.chat_a)},headers=self.headers,json={**body,'item_index':0}).status_code,422)

    def test_fetch_hotel_details_persists_and_rejects_wrong_owner(self):
        self.prepare_editable_stay()
        body={'itinerary_id':str(self.itinerary_id),'kind':'hotel','uid':self.hotel_uid,'currency':'INR'}
        async def enriched(kind, item, raw, currency, context, **kwargs):
            return {**item,'description':'Verified hotel description','images':[{'original_image':'https://hotel.example/photo.jpg'}]}, {'summary':'Hotel website details','sources':[{'url':'https://hotel.example','title':'Hotel'}],'missing':[], 'filled':['Description'], 'fetched_at':'2026-10-02T00:00:00Z'}
        with patch('trvelle.orchestrator.api_wrapper.get_orchestrator') as agent, patch('trvelle.tools.detail_lookup.DetailLookup.fetch',side_effect=enriched) as fetch:
            agent.return_value.model_router=None
            denied=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers={**self.headers,'user-id':str(self.user_b)},json=body)
            self.assertEqual(denied.status_code,404)
            fetch.assert_not_called()
            response=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body)
        self.assertEqual(response.status_code,200,response.text)
        trip=db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        hotel=next(h for h in trip['travel_options']['hotels'] if h['choose_uid']==self.hotel_uid)
        self.assertEqual(hotel['description'],'Verified hotel description')
        self.assertEqual(hotel['images'][0]['original_image'],'https://hotel.example/photo.jpg')
        self.assertIn('hotel:'+self.hotel_uid,trip['detail_reports'])

    def test_activity_research_is_invalidated_on_edit(self):
        self.prepare_editable_stay()
        body={'itinerary_id':str(self.itinerary_id),'kind':'activity','day_index':0,'item_index':1,'currency':'INR'}
        report={'summary':'Verified museum information','sources':[{'url':'https://museum.example','title':'Museum'}],'missing':['Location'],'filled':[]}
        with patch('trvelle.orchestrator.api_wrapper.get_orchestrator') as agent, patch('trvelle.tools.detail_lookup.DetailLookup.fetch',new=AsyncMock(return_value=({'title':'Visit a museum','card_type':'activity'},report))):
            agent.return_value.model_router=None
            response=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('activity:0:1',db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)['detail_reports'])
        response=self.client.post('/edit_activity',params={'chat_id':str(self.chat_a)},headers=self.headers,json={**body,'title':'Different activity'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn('activity:0:1',db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)['detail_reports'])

    def test_fetch_selected_flight_persists_without_switching_options(self):
        body=self.add_flight_options()
        self.client.post('/select_flight',params={'chat_id':str(self.chat_a)},headers=self.headers,json=body)
        report={'summary':'Airline details','sources':[],'missing':[],'filled':['Airline logos'],'fetched_at':'2026-10-02T00:00:00Z'}
        async def enriched(kind,item,raw,currency,context,**kwargs):
            return {**item,'flights':[{**item['flights'][0],'airline_logo':'https://airline.example/logo.png'}]},report
        with patch('trvelle.orchestrator.api_wrapper.get_orchestrator') as agent, patch('trvelle.tools.detail_lookup.DetailLookup.fetch',side_effect=enriched):
            agent.return_value.model_router=None
            response=self.client.post('/fetch_details',params={'chat_id':str(self.chat_a)},headers=self.headers,json={'itinerary_id':str(self.itinerary_id),'kind':'flight','uid':body['uid'],'currency':'INR'})
        self.assertEqual(response.status_code,200,response.text)
        trip=db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)
        selected=trip['travel_options']['flights'][0]['legs'][0]
        self.assertEqual(selected['price'],200)
        self.assertEqual(selected['flights'][0]['airline'],'Second')
        self.assertEqual(selected['flights'][0]['airline_logo'],'https://airline.example/logo.png')
        self.client.post('/select_flight',params={'chat_id':str(self.chat_a)},headers=self.headers,json={**body,'option_index':0})
        self.assertNotIn('flight:'+body['uid'],db_handler.get_itinerary(self.user_a,self.chat_a,self.itinerary_id)['detail_reports'])

    def test_authentication_required(self):
        response = self.client.get('/list_chats', headers={'user-id': str(self.user_a)})
        self.assertEqual(response.status_code, 401)

    def test_provider_quota_error_is_reported_in_stream(self):
        from trvelle.orchestrator.run_store import store
        run=self.client.post('/runs/start',params={'chat_id':str(self.chat_a)},headers=self.headers,json={'message':'Plan a trip'}).json()
        identifier=uuid.UUID(run['run_id'])
        store.finish(identifier,'failed','Selected model is awaiting its provider reset.')
        response=self.client.get('/runs/events',params={'run_id':run['run_id'],'after':1},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertIn('event: error',response.text)
        self.assertIn('provider reset',response.text)

    def test_flight_results_without_optional_legroom_can_be_formatted(self):
        from trvelle.tools.flight_search import FlightSearch
        airport = {'name': 'Test airport', 'id': 'BOM', 'time': '2026-12-10 10:00'}
        result = FlightSearch().format_flight_data_simple([{
            'total_duration': 60, 'type': 'One way', 'price': 2000, 'currency': 'INR',
            'flights': [{'departure_airport': airport, 'arrival_airport': airport,
                         'airline': 'Test airline', 'flight_number': 'TEST1', 'duration': 60}]
        }])
        self.assertIn('TEST1', result)
        self.assertIn('2000 INR', result)

    def test_itinerary_history_and_details(self):
        response = self.client.get('/chat_history', params={'chat_id': str(self.chat_a)}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['messages'][0]['metadata']['itineraryId'], str(self.itinerary_id))
        response = self.client.get('/tool_call', params={'chat_id': str(self.chat_a)}, headers={**self.headers, 'tool-call-id': str(self.itinerary_id)})
        self.assertEqual(response.json()['trip_name'], self.raw['trip_name'])
        self.assertEqual(response.json()['travel_options']['hotels'][0]['choose_uid'], self.hotel_uid)
        self.assertEqual(response.json()['travel_options']['hotels'][0]['images'][0]['original_image'], 'https://example.com/hotel.jpg')
        response = self.client.get('/tool_call', params={'chat_id': str(self.chat_a)}, headers={**self.headers, 'tool-call-id': self.hotel_uid})
        self.assertEqual(response.json()['properties'][0]['name'], 'Selected hotel')

    def test_cross_user_itinerary_access_is_rejected(self):
        response = self.client.get('/tool_call', params={'chat_id': str(self.chat_b)}, headers={**self.headers, 'user-id': str(self.user_b), 'tool-call-id': str(self.itinerary_id)})
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_messages_and_tool_results(self):
        response = self.client.delete('/chat', params={'chat_id': str(self.chat_a)}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        with db_handler.db_session() as db:
            self.assertIsNone(db.get(ChatSession, self.chat_a))
            self.assertIsNone(db.get(Message, self.itinerary_id))
            self.assertIsNone(db.get(ToolExecution, self.itinerary_id))

    def test_gemini_content_blocks_and_tool_calls_survive_database_reload(self):
        from langchain_core.messages import AIMessage
        call_id = "call_" + str(uuid.uuid4())
        response = AIMessage(id=str(uuid.uuid4()), name="Supervisor_Agent",
            content=[{"type": "text", "text": "Planning", "extras": {"signature": "test-signature"}}],
            tool_calls=[{"id": call_id, "name": "researcher_agent", "args": {"city": "Goa"}, "type": "tool_call"}])
        db_handler.save_message_to_db(response, {"user_id": self.user_a, "chat_id": self.chat_a})
        loaded = db_handler.load_chat_history(self.user_a, self.chat_a)[-1]
        self.assertEqual(loaded.content, response.content)
        self.assertEqual(loaded.tool_calls, response.tool_calls)
        self.assertEqual(loaded.text, "Planning")

    def test_interrupted_tool_calls_can_be_recovered_without_duplicate_messages(self):
        from langchain_core.messages import AIMessage, ToolMessage
        from trvelle.orchestrator.client import Orchestrator
        from trvelle.database.memory_manager import InMemoryChatManager
        config = {"user_id": self.user_a, "chat_id": self.chat_a}
        call_id = "call_" + str(uuid.uuid4())
        response = AIMessage(id=str(uuid.uuid4()), content="", name="Supervisor_Agent",
            tool_calls=[{"id": call_id, "name": "researcher_agent", "args": {}, "type": "tool_call"}])
        db_handler.save_message_to_db(response, config)
        agent = Orchestrator.__new__(Orchestrator)
        agent.chat_manager = InMemoryChatManager()
        agent.chat_manager.set_chat_history(agent._get_chat_history_key(config), db_handler.load_chat_history(self.user_a, self.chat_a))
        agent.recover_failed_turn(config)
        agent.recover_failed_turn(config)
        results = [message for message in db_handler.load_chat_history(self.user_a, self.chat_a) if isinstance(message, ToolMessage) and message.tool_call_id == call_id]
        self.assertEqual(len(results), 1)

    def test_itinerary_dates_are_json_serializable(self):
        model = Itinerary.model_validate({'trip_name': 'Test', 'summary': {'dates': {'start': '2026-11-10', 'end': '2026-11-12'}}, 'daily_plan': []})
        result = asyncio.run(itinerary_tool(model))
        serialized = json.loads(json.dumps(result['raw']))
        self.assertEqual(serialized['summary']['dates']['start'], '2026-11-10')

    def test_overnight_trip_requires_real_hotel_and_selected_card(self):
        trip={'summary':{'dates':{'start':'2026-11-15','end':'2026-11-19'}},'daily_plan':[{'day':1,'items':[]}]}
        self.assertIn('selected hotel card',db_handler.itinerary_search_issue(self.user_a,self.chat_a,trip))
        with db_handler.db_session() as db:
            db.query(ToolExecution).filter_by(chat_id=self.chat_a,tool_name='hotel_search').delete()
            db.commit()
        self.assertIn('researcher_agent',db_handler.itinerary_search_issue(self.user_a,self.chat_a,trip))
        with db_handler.db_session() as db:
            db.add(Message(message_id=uuid.uuid4(),chat_id=self.chat_a,user_id=self.user_a,type='human',content='I am staying with family, skip hotels.'))
            db.commit()
        self.assertIsNone(db_handler.itinerary_search_issue(self.user_a,self.chat_a,trip))

    def test_partial_itinerary_allows_missing_hotels_but_still_rejects_invented_offers(self):
        trip={'planning_status':'partial','unfinished':['Hotels'],
            'summary':{'dates':{'start':'2027-03-14','end':'2027-03-20'}},'daily_plan':[{'day':1,'items':[]}]}
        with db_handler.db_session() as db:
            db.query(ToolExecution).filter_by(chat_id=self.chat_a,tool_name='hotel_search').delete()
            db.commit()
        self.assertIsNone(db_handler.itinerary_search_issue(self.user_a,self.chat_a,trip))
        trip['daily_plan'][0]['items']=[{'card_type':'hotel','uid':'invented-hotel'}]
        self.assertIn('exact choose_uid',db_handler.itinerary_search_issue(self.user_a,self.chat_a,trip))

    def test_combined_city_research_gets_the_same_bounded_allowance_as_separate_segments(self):
        from unittest.mock import Mock
        from trvelle.orchestrator.client import Orchestrator
        from trvelle.orchestrator.run_store import store
        from langchain_core.messages import ToolMessage
        run=store.create(self.user_a,self.chat_a,{'message':'Plan Rome and Florence','currency':'INR',
            'choices':{},'search_limits':{'serpapi':6,'tavily':6,'brave':12}})
        agent=object.__new__(Orchestrator)
        agent.sequential_researchers=True;agent.chat_manager=Mock()
        agent.chat_manager.get_chat_history.return_value=[]
        agent.orchestrate_research=AsyncMock(return_value=ToolMessage(content='Saved city research',tool_call_id='done'))
        config={'user_id':self.user_a,'chat_id':self.chat_a,'run_id':uuid.UUID(run['run_id']),
            'search_limits':{'serpapi':6,'tavily':6,'brave':12}}
        asyncio.run(agent.run_research_tasks.__wrapped__(agent,[{'id':'research','args':{
            'city':'Rome and Florence','segment_number':1}}],config))
        self.assertEqual(store.snapshot(self.user_a,uuid.UUID(run['run_id']))['search_limits'],
            {'serpapi':12,'tavily':12,'brave':12})

    def test_failed_publication_preserves_the_full_researched_schedule_as_a_draft(self):
        from types import SimpleNamespace
        from trvelle.orchestrator.worker import publish_researched_draft
        from trvelle.orchestrator.run_store import store
        run=store.create(self.user_a,self.chat_a,{'message':'Plan Italy','currency':'INR','choices':{},'search_limits':{'serpapi':6,'tavily':6}})
        raw={'trip_name':'Italy researched plan','summary':{'dates':{'start':'2027-03-14','end':'2027-03-15'},'currency':'INR'},
            'daily_plan':[{'day':1,'destination':'Rome','items':[{'card_type':'activity','title':'Colosseum','location':'Rome','start_time':'09:00'}]},
                          {'day':2,'destination':'Rome','items':[{'card_type':'activity','title':'Vatican Museums','location':'Rome','start_time':'09:00'}]}]}
        with db_handler.db_session() as db:
            db.add(Message(message_id=uuid.uuid4(),chat_id=self.chat_a,user_id=self.user_a,type='ai',content='',
                additional_kwargs={'tool_calls':[{'name':'itinerary_tool','args':{'itinerary':raw},'id':'rejected'}]}))
            db.commit()
        config={'user_id':self.user_a,'chat_id':self.chat_a,'run_id':uuid.UUID(run['run_id']),'currency':'INR'}
        agent=SimpleNamespace(db_handler=db_handler)
        async def attach_photo(trip):
            trip['daily_plan'][0]['items'][0]['image_url']='https://example.test/colosseum.jpg'
            return trip
        with patch('trvelle.orchestrator.worker.enrich_itinerary',new=AsyncMock(side_effect=attach_photo)) as enrich:
            self.assertTrue(asyncio.run(publish_researched_draft(agent,config,'Missing hotel quotes')))
        enrich.assert_awaited_once()
        snapshot=store.snapshot(self.user_a,config['run_id'])
        saved=db_handler.get_itinerary(self.user_a,self.chat_a,uuid.UUID(snapshot['itinerary_id']))
        self.assertEqual(saved['planning_status'],'partial')
        self.assertEqual([day['items'][0]['title'] for day in saved['daily_plan']],['Colosseum','Vatican Museums'])
        self.assertEqual(saved['daily_plan'][0]['items'][0]['image_url'],'https://example.test/colosseum.jpg')
        self.assertFalse(asyncio.run(publish_researched_draft(agent,config,'Retry')))

    def test_failed_flight_search_blocks_itinerary_finalization(self):
        with db_handler.db_session() as db:
            db.add(Message(message_id=uuid.uuid4(), chat_id=self.chat_a, user_id=self.user_a,
                type='tool', message_name='flight_search', content='Search failed'))
            db.commit()
        issue = db_handler.itinerary_search_issue(self.user_a, self.chat_a, {'daily_plan': []})
        self.assertIn('Flight searches failed', issue)

    def test_invented_hotel_uid_is_rejected(self):
        trip = {'daily_plan': [{'items': [{'card_type': 'hotel', 'uid': 'invented-hotel'}]}]}
        self.assertIn('exact choose_uid', db_handler.itinerary_search_issue(self.user_a, self.chat_a, trip))
        trip['daily_plan'][0]['items'][0]['uid'] = self.hotel_uid
        self.assertIsNone(db_handler.itinerary_search_issue(self.user_a, self.chat_a, trip))

    def test_recovered_flight_logos_are_included_in_existing_itinerary(self):
        with db_handler.db_session() as db:
            db.add(ToolExecution(message_id=uuid.uuid4(), chat_id=self.chat_a, tool_name='flight_search',
                unique_identifier=str(self.itinerary_id), created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                raw_response=[{'search_parameters': {'currency': 'INR'}, 'best_flights': [{'price': 2000,
                    'flights': [{'airline': 'Test', 'airline_logo': 'https://example.com/airline.png'}]}]}, {'choose_uid': 'real-flight'}]))
            db.commit()
        trip = db_handler.get_itinerary(self.user_a, self.chat_a, self.itinerary_id)
        flight = trip['travel_options']['flights'][0]
        self.assertEqual(flight['uid'], 'real-flight')
        self.assertEqual(flight['legs'][0]['flights'][0]['airline_logo'], 'https://example.com/airline.png')

if __name__ == '__main__':
    unittest.main()
