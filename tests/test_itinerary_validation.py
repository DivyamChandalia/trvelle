import unittest
from trvelle.tools.itinerary_tool import Itinerary
from trvelle.utils.itinerary_validation import validate_trip
class ItineraryValidationTests(unittest.TestCase):
 def test_hotel_coverage_excludes_outbound_flight_night_but_requires_final_city_night(self):
  plan={'summary':{'dates':{'start':'2027-03-14','end':'2027-03-20'}},'daily_plan':[],
        'travel_options':{'flights':[{'selected':True,'legs':[{'flights':[{'arrival_airport':{'time':'2027-03-15 05:55'}}]},
           {'flights':[{'departure_airport':{'time':'2027-03-20 12:20'},'arrival_airport':{'time':'2027-03-21 04:15'}}]}]}],
         'hotels':[{'selected':True,'check_in_date':'2027-03-15','check_out_date':'2027-03-17'},
                   {'selected':True,'check_in_date':'2027-03-17','check_out_date':'2027-03-19'}]}}
  missing=[issue['date'] for issue in validate_trip(plan)['issues'] if issue['code']=='hotel_missing']
  self.assertEqual(missing,['2027-03-19'])
 def test_schema_accepts_legacy_visit_notes_and_exposes_the_canonical_field(self):
  from trvelle.tools.itinerary_tool import PlannedItem
  item=PlannedItem.model_validate({'title':'Museum','visitor_details':{'booking':'Timed entry recommended'},'source_url':'https://museum.example'}).model_dump()
  self.assertEqual(item['visitor_information'],{'booking':'Timed entry recommended'})
  self.assertNotIn('visitor_details',item)
  self.assertIn('visitor_information',PlannedItem.model_json_schema()['properties'])
  self.assertIsNone(PlannedItem(title='River stroll').visitor_information)
 def test_schema_preserves_suggested_times_and_location(self):
  plan=Itinerary.model_validate({'trip_name':'Singapore','summary':{'dates':{'start':'2026-11-15','end':'2026-11-15'},'travelers':2},'daily_plan':[{'day':1,'items':[{'description':'Botanic gardens','time':'10:00','end_time':'12:00','location':'Singapore Botanic Gardens'}]}]}).model_dump(mode='json')
  item=plan['daily_plan'][0]['items'][0]
  self.assertEqual(item['start_time'],'10:00');self.assertEqual(item['location'],'Singapore Botanic Gardens')
 def test_round_trip_party_price_is_counted_once_and_conflicts_are_visible(self):
  plan={'summary':{'dates':{'start':'2026-11-15','end':'2026-11-16'},'currency':'INR','budget_amount':100},'daily_plan':[{'day':1,'items':[{'card_type':'activity','title':'Museum','start_time':'09:00','end_time':'08:00'}]},{'day':2,'items':[]}],'travel_options':{'flights':[{'selected':True,'legs':[{'price':200,'currency':'INR','flights':[{'departure_airport':{'time':'2026-11-15 12:00'},'arrival_airport':{'time':'2026-11-16 04:00'}}]},{'price':300,'currency':'INR','flights':[{'departure_airport':{'time':'2026-11-17 10:00'},'arrival_airport':{'time':'2026-11-17 15:00'}}]}]}]}}
  report=validate_trip(plan)
  self.assertEqual(report['priced_total'],300)
  codes={x['code'] for x in report['issues']}
  self.assertTrue({'late_arrival','return_date','time_order','over_budget'}.issubset(codes))
 def test_unverified_twin_room_and_budget_gap_are_not_reported_as_compliant(self):
  plan={'summary':{'dates':{'start':'2026-11-15','end':'2026-11-15'},'currency':'INR','budget_amount':10000},'requirements':{'private_room':True,'bed':'double','minimum_rating':4},'daily_plan':[{'items':[]}],'travel_options':{'hotels':[{'name':'Private twin studio','selected':True,'currency':'INR','total_rate':{'extracted_lowest':9200}}]}}
  codes={issue['code'] for issue in validate_trip(plan)['issues']}
  self.assertTrue({'room_unverified','bed_mismatch','hotel_rating','unpriced_budget'}.issubset(codes))
