import unittest
from unittest.mock import AsyncMock
from copy import deepcopy
from trvelle.tools.flight_booking import fetch_flight_booking,booking_offers,safe_booking_url


def journey():
 return [{'search_parameters':{'departure_id':'BOM','arrival_id':'NRT','outbound_date':'2027-04-22','return_date':'2027-05-03','adults':6,'currency':'INR','api_key':'never-forward-fixture'},'best_flights':[{'price':400000,'booking_token':'saved-full-journey-fixture','flights':[{'flight_number':'NH 830','departure_airport':{'id':'BOM','time':'2027-04-22 19:30'},'arrival_airport':{'id':'NRT','time':'2027-04-23 07:25'}}]}],'selected_option_index':0}]

class FlightBookingTests(unittest.IsolatedAsyncioTestCase):
 async def test_booking_lookup_is_exact_cached_and_does_not_change_saved_fare(self):
  raw=journey();selected=deepcopy(raw[0]['best_flights'][0])
  lookup=type('Lookup',(),{'serp':AsyncMock(return_value={'selected_flights':[selected],'booking_options':[{'together':{'book_with':'ANA','price':412000,'booking_request':{'url':'https://www.google.com/travel/clk/f','post_data':'u=booking-fixture'}}}]})})()
  updated,report=await fetch_flight_booking(raw,'INR',lookup)
  self.assertEqual(report['status'],'complete');self.assertEqual(updated[0]['best_flights'][0]['price'],400000)
  self.assertEqual(booking_offers(updated)[0]['currency'],'INR')
  params=lookup.serp.call_args.args[0]
  self.assertEqual(params['adults'],6);self.assertEqual(params['booking_token'],'saved-full-journey-fixture');self.assertNotIn('api_key',params)
  await fetch_flight_booking(updated,'INR',lookup);lookup.serp.assert_awaited_once()
  self.assertNotIn('booking_options',raw[0]['best_flights'][0])
 async def test_different_flight_is_rejected_without_changing_itinerary(self):
  wrong=deepcopy(journey()[0]['best_flights'][0]);wrong['flights'][0]['flight_number']='NH 999'
  lookup=type('Lookup',(),{'serp':AsyncMock(return_value={'selected_flights':[wrong],'booking_options':[]})})()
  with self.assertRaises(ValueError):await fetch_flight_booking(journey(),'INR',lookup)
 async def test_no_token_does_not_spend_search_credits_or_fabricate_links(self):
  raw=journey();del raw[0]['best_flights'][0]['booking_token']
  lookup=type('Lookup',(),{'serp':AsyncMock()})()
  updated,report=await fetch_flight_booking(raw,'INR',lookup)
  lookup.serp.assert_not_awaited();self.assertEqual(report['status'],'unavailable');self.assertEqual(booking_offers(updated),[])
 def test_unsafe_provider_targets_are_not_booking_options(self):
  for url in ['javascript:alert(1)','http://site.example','https://user:password@site.example','https://127.0.0.1/','https://169.254.169.254/','https://localhost/']:
   self.assertIsNone(safe_booking_url(url))
