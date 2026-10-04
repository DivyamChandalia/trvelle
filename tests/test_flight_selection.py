import unittest
from unittest.mock import patch
from trvelle.tools.flight_selection import choose_flight

class FlightSelectionTests(unittest.TestCase):
 def test_return_leg_change_preserves_outbound(self):
  raw=[{'best_flights':[{'price':100,'departure_token':'a'}]}, {'best_flights':[{'price':120},{'price':150}]},{'choose_uid':'uid'}]
  result=choose_flight(raw,1,1)
  self.assertEqual(result[0],raw[0])
  self.assertEqual(result[1]['selected_option_index'],1)
  self.assertEqual(raw[1].get('selected_option_index'),None)
 def test_outbound_change_refreshes_return_options_for_that_token(self):
  raw=[{'search_parameters':{'currency':'INR'},'best_flights':[{'price':100,'departure_token':'a'},{'price':110,'departure_token':'b'}]}, {'best_flights':[{'price':120}]},{'choose_uid':'uid'}]
  with patch('trvelle.tools.flight_selection.searcher._fetch_results',return_value={'best_flights':[{'price':200}],'search_parameters':{'currency':'INR'}}) as fetch:
   result=choose_flight(raw,0,1)
  self.assertEqual(fetch.call_args.args[0]['departure_token'],'b')
  self.assertEqual(result[1]['best_flights'][0]['price'],200)
  self.assertEqual(result[0]['selected_option_index'],1)
  self.assertNotIn('api_key',result[0]['search_parameters'])
 def test_invalid_index_rejected(self):
  with self.assertRaises(ValueError):choose_flight([{'best_flights':[{'price':100}]}],0,50)
 def test_missing_dependent_return_does_not_reuse_old_return(self):
  raw=[{'best_flights':[{'price':100,'departure_token':'a'},{'price':110,'departure_token':'b'}]}, {'best_flights':[{'price':120}]},{'choose_uid':'uid'}]
  with patch('trvelle.tools.flight_selection.searcher._fetch_results',return_value={}):
   with self.assertRaises(ValueError):choose_flight(raw,0,1)

class CurrencyTests(unittest.IsolatedAsyncioTestCase):
 async def test_existing_dollar_quotes_convert_without_losing_original(self):
  from trvelle.utils.currency import present_currency
  source={'travel_options':{'hotels':[{'rate_per_night':{'lowest':'$100','extracted_lowest':100}}], 'flights':[{'legs':[{'price':500,'currency':'INR'}]}]}}
  with patch('trvelle.utils.currency.exchange_rate',return_value=(80,'2026-10-02')):
   result=await present_currency(source,'INR')
  rate=result['travel_options']['hotels'][0]['rate_per_night']
  self.assertEqual(rate['lowest'],'₹8,000')
  self.assertEqual(rate['original_quote']['lowest'],'$100')
  self.assertEqual(source['travel_options']['hotels'][0]['rate_per_night']['lowest'],'$100')
  self.assertEqual(result['travel_options']['flights'][0]['legs'][0]['price'],500)
 async def test_missing_exchange_rate_does_not_relabel_dollars(self):
  from trvelle.utils.currency import present_currency
  with patch('trvelle.utils.currency.exchange_rate',side_effect=ValueError('unavailable')):
   result=await present_currency({'rate_per_night':{'lowest':'$100','extracted_lowest':100}},'INR')
  self.assertEqual(result['rate_per_night']['lowest'],'Price unavailable')
 def test_indian_origin_detection(self):
  from trvelle.utils.currency import preferred_currency
  self.assertEqual(preferred_currency('Bangalore'),'INR')
  self.assertEqual(preferred_currency('Mumbai, India'),'INR')
  self.assertEqual(preferred_currency('BLR'),'INR')
  self.assertEqual(preferred_currency('London'),'USD')
 async def test_converted_hotel_totals_and_budget_use_the_same_currency(self):
  from trvelle.utils.currency import present_currency
  source={'summary':{'dates':{'start':'2026-11-15','end':'2026-11-15'},'currency':'SGD','budget_amount':100},'daily_plan':[{'items':[]}], 'validation':{}, 'travel_options':{'hotels':[{'selected':True,'currency':'SGD','total_rate':{'lowest':'$80','extracted_lowest':80}}]}}
  with patch('trvelle.utils.currency.exchange_rate',return_value=(60,'2026-10-03')) as fx:
   result=await present_currency(source,'INR')
  self.assertEqual(fx.call_args.args,('SGD','INR'))
  self.assertEqual(result['validation']['priced_total'],4800)
  self.assertEqual(result['validation']['budget_amount'],6000)
  self.assertEqual(source['travel_options']['hotels'][0]['currency'],'SGD')
