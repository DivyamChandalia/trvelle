import unittest
from trvelle.utils.itinerary_edits import hotel_stay_key,replace_hotel,edit_activity

class EditTests(unittest.TestCase):
 def test_hotel_selection_rewrites_the_stay_without_changing_activity_kind(self):
  trip={'daily_plan':[{'day':1,'items':[{'title':'Stay at Original Hotel','description':'Check into Original Hotel','uid':'old'},{'card_type':'activity','title':'Dinner at Original Hotel'}]}]}
  hotels=[{'choose_uid':'old','name':'Original Hotel','stay_key':'venice','mentioned_in_plan':True},{'choose_uid':'new','name':'New Hotel','stay_key':'venice'}]
  result=replace_hotel(trip,hotels,'venice','new')
  self.assertEqual(result['daily_plan'][0]['items'][0]['uid'],'new')
  self.assertEqual(result['daily_plan'][0]['items'][0]['title'],'Stay at New Hotel')
  self.assertNotIn('uid',result['daily_plan'][0]['items'][1])
  self.assertEqual(result['daily_plan'][0]['items'][1]['title'],'Dinner at New Hotel')
  self.assertEqual(result['hotel_selections']['venice'],'new')
  self.assertEqual(trip['daily_plan'][0]['items'][0]['uid'],'old')
 def test_hotel_selection_rejects_wrong_destination(self):
  with self.assertRaises(ValueError):replace_hotel({},[{'choose_uid':'capri','stay_key':'capri'}],'venice','capri')
 def test_dates_are_part_of_the_stay_identity(self):
  self.assertNotEqual(hotel_stay_key({'q':'Venice','check_in_date':'2026-12-10'}),hotel_stay_key({'q':'Venice','check_in_date':'2026-12-11'}))
 def test_activity_edit_invalidates_changed_place_images_and_allows_activity_at_hotel(self):
  item={'card_type':'activity','title':'Dinner at Hotel One','image_url':'https://example.com/photo.jpg'}
  result=edit_activity({'daily_plan':[{'items':[item]}]},0,0,{'title':'New dinner','location':'Piazza'},['Hotel One'],[])
  self.assertNotIn('image_url',result['daily_plan'][0]['items'][0])
  self.assertEqual(result['daily_plan'][0]['items'][0]['title'],'New dinner')
  self.assertEqual(item['title'],'Dinner at Hotel One')
 def test_activity_edit_cannot_change_hotel_or_flight(self):
  for card_type in ['hotel','flight']:
   with self.assertRaises(ValueError):edit_activity({'daily_plan':[{'items':[{'card_type':card_type}]}]},0,0,{'title':'new'})
 def test_changed_activity_discards_previous_place_visit_notes_and_sources(self):
  item={'card_type':'activity','title':'Uffizi','visitor_details':{'booking':'Timed entry'},'visitor_information':'Old visit notes','source_url':'https://uffizi.it','visitor_information_sources':[{'url':'https://uffizi.it'}]}
  trip={'daily_plan':[{'items':[item]}],'detail_reports':{'activity:0:0':{'summary':'Old research'}}}
  result=edit_activity(trip,0,0,{'title':'Accademia'})
  self.assertFalse(any(key in result['daily_plan'][0]['items'][0] for key in ('visitor_details','visitor_information','source_url','visitor_information_sources')))
  self.assertNotIn('activity:0:0',result['detail_reports'])
  self.assertIn('visitor_details',item)
