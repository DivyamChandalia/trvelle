import copy
import unittest
from langchain_core.messages import AIMessage, ToolMessage
from trvelle.utils.party_prices import project_prices, hotel_price
from trvelle.utils.budget import budget_breakdown
from trvelle.utils.breakfast import apply_breakfast_rules
from trvelle.utils.itinerary_patch import ItineraryPatch, apply_patch, update_scope
from trvelle.utils.item_alternatives import alternative_fits, replace_item_alternative


class PartyUpdateTests(unittest.TestCase):
    def test_destination_calendar_starts_at_landing_preserves_flights_and_cached_item_details(self):
        from trvelle.utils.destination_days import destination_calendar
        trip=self.trip();trip['summary']['dates']={'start':'2027-04-24','end':'2027-04-26'}
        trip['daily_plan'].insert(0,{'day':1,'date':'2027-04-24','items':[{'card_type':'flight','uid':'journey'}]})
        trip['daily_plan'].append({'day':3,'date':'2027-04-26','items':[]})
        trip['detail_reports']={'activity:1:0:Ticket price':{'summary':'Saved museum price'}}
        trip['travel_options']={'flights':[{'selected':True,'uid':'journey','legs':[
            {'flights':[{'departure_airport':{'id':'BOM','time':'2027-04-24 23:35'},'arrival_airport':{'id':'NRT','time':'2027-04-25 15:55'}}]},
            {'flights':[{'departure_airport':{'id':'NRT','time':'2027-04-26 09:00'},'arrival_airport':{'id':'BOM','time':'2027-04-26 18:00'}}]}]}]}
        result=destination_calendar(trip)
        self.assertEqual(result['summary']['dates']['start'],'2027-04-25')
        self.assertEqual(result['summary']['travel_dates']['start'],'2027-04-24')
        self.assertEqual([day['day'] for day in result['daily_plan']],[1,2])
        self.assertEqual(result['daily_plan'][0]['items'][0]['uid'],'journey')
        self.assertIn('activity:0:1:Ticket price',result['detail_reports'])
        self.assertEqual(result['travel_options']['flights'][0]['legs'][0]['itinerary_date'],'2027-04-25')
    def trip(self):
        return {'itinerary_id':'saved-plan','revision':1,'summary':{'currency':'INR','travelers':6},'source_search_ids':['flight-source'],
                'daily_plan':[{'day':1,'date':'2027-04-25','destination':'Tokyo','items':[
                    {'item_id':'museum','card_type':'activity','title':'Museum','location':'Tokyo museum district','start_time':'09:00','end_time':'11:00','cost':{'min_price':800,'max_price':1600,'currency':'INR','scope':'per_person','status':'estimate'}},
                    {'item_id':'lunch','card_type':'meal','title':'Lunch break','start_time':'12:00','end_time':'13:30'}]}]}

    def test_party_ranges_and_budget_children_share_totals(self):
        trip=self.trip();project_prices(trip)
        price=trip['daily_plan'][0]['items'][0]['price_summary']
        self.assertEqual((price['total_cost']['min_price'],price['total_cost']['max_price']),(4800,9600))
        budget=budget_breakdown(trip);row=next(r for r in budget['rows'] if r['key']=='activities')
        self.assertEqual(row['known'],9600)
        self.assertEqual(row['children'][0]['price_summary'],price)
        self.assertEqual(trip['daily_plan'][0]['items'][0]['cost']['max_price'],1600)

    def test_room_assumptions_and_confirmed_group_quotes_do_not_double_count(self):
        hotel={'stay_key':'tokyo','currency':'INR','check_in_date':'2027-04-25','check_out_date':'2027-04-28','total_rate':{'extracted_lowest':3000}}
        price=hotel_price(hotel,self.trip())
        self.assertEqual((price['rooms'],price['people'],price['nights']),(3,6,3))
        self.assertEqual(price['total_cost']['price'],9000)
        self.assertEqual(price['total_cost']['status'],'estimate')
        hotel.update(quote_scope='party',quoted_rooms=3)
        self.assertEqual(hotel_price(hotel,self.trip())['total_cost']['price'],3000)
        self.assertEqual(hotel_price(hotel,self.trip())['total_cost']['status'],'quoted')

    def test_breakfast_amenities_are_not_rate_inclusion_and_external_meals_stay_priced(self):
        trip=self.trip();trip['daily_plan'][0]['items']=[{'card_type':'meal','title':'Breakfast at hotel','dining':{'meal_type':'breakfast','at_hotel':True},'cost':{'price':0,'currency':'INR','scope':'party','status':'quoted'}}]
        hotel={'selected':True,'check_in_date':'2027-04-24','check_out_date':'2027-04-28','amenities':['Free breakfast'],'currency':'INR'}
        trip['travel_options']={'hotels':[hotel]}
        apply_breakfast_rules(trip)
        self.assertNotIn('cost',trip['daily_plan'][0]['items'][0])
        self.assertEqual(hotel['breakfast']['status'],'offered')
        hotel['selected_booking_offer']={'breakfast_included':True,'link':'https://hotel.example/rate'}
        apply_breakfast_rules(trip)
        self.assertEqual(trip['daily_plan'][0]['items'][0]['cost']['price'],0)
        self.assertTrue(trip['daily_plan'][0]['items'][0]['dining']['breakfast_included'])
        trip['daily_plan'][0]['items'].append({'card_type':'meal','title':'Local cafe','dining':{'meal_type':'breakfast','venue_name':'Local cafe'},'cost':{'price':1000,'currency':'INR','scope':'per_person','status':'estimate'}})
        apply_breakfast_rules(trip)
        self.assertEqual(trip['daily_plan'][0]['items'][-1]['cost']['price'],1000)

    def test_food_patch_keeps_unrelated_items_and_source_quotes(self):
        trip=self.trip();original=copy.deepcopy(trip)
        patch=ItineraryPatch.model_validate({'operations':[{'action':'replace_item','day_index':0,'item_id':'lunch','item':{'card_type':'meal','title':'Ramen shop','location':'Tokyo','start_time':'01:00','end_time':'02:00','cost':{'price':1200,'currency':'INR','scope':'per_person','status':'estimate'}}}]})
        result=apply_patch(trip,patch,'food')
        self.assertEqual(result['daily_plan'][0]['items'][0],original['daily_plan'][0]['items'][0])
        self.assertEqual(result['daily_plan'][0]['items'][1]['start_time'],'12:00')
        self.assertEqual(result['source_search_ids'],['flight-source'])
        self.assertEqual(trip,original)
        patch.operations[0].item_id='museum'
        with self.assertRaises(ValueError):apply_patch(trip,patch,'food')

    def test_append_patch_cannot_overlap_existing_visit(self):
        patch=ItineraryPatch.model_validate({'operations':[{'action':'append_item','day_index':0,'item':{'card_type':'meal','title':'Cafe','start_time':'10:00','end_time':'11:00'}}]})
        with self.assertRaises(ValueError):apply_patch(self.trip(),patch,'food')

    def test_alternatives_must_fit_slot_and_route_and_keep_sources_after_swap(self):
        trip=self.trip();item=trip['daily_plan'][0]['items'][0]
        candidate={'alternative_id':'alt','card_type':'activity','title':'Small gallery','location':'Tokyo museum district','duration_minutes':60,'source_url':'https://gallery.example','image_url':'https://gallery.example/photo.jpg','cost':{'price':500,'currency':'INR','scope':'per_person','status':'quoted'}}
        self.assertTrue(alternative_fits(item,candidate))
        self.assertFalse(alternative_fits(item,{**candidate,'duration_minutes':150}))
        self.assertFalse(alternative_fits(item,{**candidate,'location':'Kyoto'}))
        item['alternatives']=[candidate]
        result=replace_item_alternative(trip,'museum','alt')
        selected=result['daily_plan'][0]['items'][0]
        self.assertEqual(selected['source_url'],'https://gallery.example')
        self.assertEqual(selected['image_url'],'https://gallery.example/photo.jpg')
        self.assertEqual(selected['start_time'],'09:00')
        self.assertEqual(selected['alternatives'][0]['title'],'Museum')

    def test_prompt_driven_update_scope_does_not_turn_new_trips_into_patches(self):
        self.assertEqual(update_scope('Recommend food places near my hotel'),'food')
        self.assertEqual(update_scope('Plan a dinner near the museum'),'food')
        self.assertEqual(update_scope('More alternative activities'),'activities')
        self.assertIsNone(update_scope('Plan a 7 day food trip to Italy'))
        self.assertEqual(update_scope('Find cheaper flights and food places'),'inventory')
        self.assertEqual(update_scope('Make the museum visit later'),'general')
        self.assertEqual(update_scope('Set the trip budget to 300000 INR'),'general')
        self.assertEqual(update_scope('Change the return date'),'inventory')
        self.assertEqual(update_scope('Move dinner to 8pm'),'general')
        self.assertEqual(update_scope('Reduce the food budget'),'general')
        self.assertEqual(update_scope('Make this activity later'),'general')

    def test_interrupted_multi_tool_turn_keeps_pending_call(self):
        from trvelle.orchestrator.itinerary_updates import pending_call_message
        ai=AIMessage(content='',tool_calls=[{'id':'one','name':'research_update','args':{}},{'id':'two','name':'research_update','args':{}}])
        prompt=[ai,ToolMessage(content='Saved result',tool_call_id='one')]
        self.assertIs(pending_call_message(prompt),ai)
        prompt.append(ToolMessage(content='Saved result',tool_call_id='two'))
        self.assertIsNone(pending_call_message(prompt))

    def test_general_time_edit_preserves_cached_cost_and_media_and_context_diff_is_small(self):
        from trvelle.utils.edit_context import context_snapshot,edit_diff
        trip=self.trip();trip['daily_plan'][0]['items'][0]['image_url']='https://museum.example/photo'
        patch=ItineraryPatch.model_validate({'operations':[{'action':'replace_item','day_index':0,'item_id':'museum','item':{'card_type':'activity','start_time':'09:30','end_time':'11:00'}}]})
        result=apply_patch(trip,patch,'general')
        edited=result['daily_plan'][0]['items'][0]
        self.assertEqual(edited['cost'],trip['daily_plan'][0]['items'][0]['cost'])
        self.assertEqual(edited['image_url'],trip['daily_plan'][0]['items'][0]['image_url'])
        base=context_snapshot(trip);delta=edit_diff(base,context_snapshot(result))
        self.assertIn({'op':'replace','path':'/daily_plan/0/items/0/start_time','value':'09:30'},delta)
        self.assertFalse(any('cost' in change['path'] or 'image_url' in change['path'] for change in delta))

    def test_trip_duration_edit_adds_only_requested_day_and_preserves_other_days(self):
        trip=self.trip();trip['summary']['dates']={'start':'2027-04-25','end':'2027-04-25'}
        patch=ItineraryPatch.model_validate({'summary_changes':{'dates':{'start':'2027-04-25','end':'2027-04-26'}},'day_actions':[{'action':'append_day','day':{'day':2,'date':'2027-04-26','destination':'Tokyo','items':[{'card_type':'free_time','title':'A relaxed morning'}]}}]})
        result=apply_patch(trip,patch,'inventory')
        self.assertEqual(len(result['daily_plan']),2)
        self.assertEqual(result['daily_plan'][0]['items'][0]['cost'],trip['daily_plan'][0]['items'][0]['cost'])
        self.assertEqual(result['summary']['dates']['end'],'2027-04-26')
        with self.assertRaises(ValueError):apply_patch(trip,patch,'food')
