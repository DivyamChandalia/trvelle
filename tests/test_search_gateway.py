"""Persistent cache, concurrency, replay and spend caps without provider traffic."""
import asyncio, json, os, tempfile, time, unittest, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
from trvelle.database import DBHandler
from trvelle.database.models import SearchCache,SearchAccount,SearchCharge,SearchProviderState
from trvelle.tools.search_gateway import SearchGateway,SearchBudgetError,BraveResponseError,normalized_query,query_key,search_context,ttl

class GatewayTests(unittest.TestCase):
 def setUp(self):
  self.key=uuid.uuid4().hex
  self.env=patch.dict(os.environ,{'SERPAPI_API_KEY':self.key,'TAVILY_API_KEY':self.key,'BRAVE_API_KEY':self.key,'BRAVE_REQUESTS_PER_SECOND':'20','TRVELLE_SEARCH_MODE':'live'})
  self.env.start()
  self.calls=[]
  def send(provider,params,key):
   self.calls.append((provider,params));time.sleep(.04);return {'properties':[{'name':'Verified hotel','images':[{'original_image':'https://example.test/photo.jpg'}]}]}
  self.gateway=SearchGateway(DBHandler.db_session,DBHandler.engine,send)
  self.gateway.provider_usage=Mock(return_value={'used':50,'limit':1000,'reset_at':datetime.now(timezone.utc)+timedelta(days=20)})
  self.scope=self.gateway.identity('serpapi')[1]
 def tearDown(self):
  with DBHandler.db_session() as db:
   for model in (SearchCharge,SearchCache,SearchAccount,SearchProviderState):db.query(model).filter_by(scope=self.scope).delete()
   db.commit()
  self.env.stop()
 def test_normalization_is_scoped_and_filter_sensitive(self):
  self.assertEqual(query_key('serpapi',{'q':' Singapore  Hotels ','api_key':'ignored'}),query_key('serpapi',{'q':'singapore hotels'}))
  self.assertNotEqual(query_key('serpapi',{'q':'singapore'},'a'),query_key('serpapi',{'q':'singapore'},'b'))
  self.assertNotEqual(query_key('serpapi',{'q':'singapore','adults':1}),query_key('serpapi',{'q':'singapore','adults':2}))
  self.assertEqual(ttl('serpapi',{'engine':'google_flights'}),900)
 def test_concurrent_same_query_spends_once_and_retains_images(self):
  with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.gateway.request('serpapi',{'engine':'google_hotels','q':'Singapore'}),range(2)))
  self.assertEqual(len(self.calls),1)
  self.assertEqual(results[0]['properties'][0]['images'][0]['original_image'],'https://example.test/photo.jpg')
  with DBHandler.db_session() as db:self.assertEqual(db.query(SearchCharge).filter_by(scope=self.scope).count(),1)
 def test_cache_normalization_preserves_search_operator_meaning(self):
  params={'engine':'google','q':'Uffizi site:UFFIZI.IT (hours OR tickets)'}
  self.assertEqual(normalized_query('serpapi',params)['q'],'uffizi site:uffizi.it (hours OR tickets)')
  self.assertNotEqual(query_key('serpapi',params),query_key('serpapi',{**params,'q':params['q'].lower()}))
  self.gateway.request('serpapi',params)
  self.assertIn(' OR ',self.calls[0][1]['q'])
 def test_local_task_budget_counts_each_uncached_request(self):
  with search_context(limits={'serpapi':1,'tavily':1}):
   self.gateway.request('serpapi',{'q':'first'})
   self.gateway.request('serpapi',{'q':'first'})
   with self.assertRaises(SearchBudgetError):self.gateway.request('serpapi',{'q':'second'})
  self.assertEqual(len(self.calls),1)
 def test_uncertain_paid_request_is_not_repeated(self):
  self.gateway.transport=Mock(side_effect=TimeoutError())
  for _ in range(2):
   with self.assertRaises(SearchBudgetError):self.gateway.request('serpapi',{'q':'uncertain'})
  self.assertEqual(self.gateway.transport.call_count,1)
 def test_replay_never_reads_credentials_or_calls_transport(self):
  with tempfile.TemporaryDirectory() as directory:
   params={'query':'replay only'}
   Path(directory,query_key('tavily',params)+'.json').write_text(json.dumps({'results':[{'url':'https://example.test','content':'Saved evidence'}]}))
   with patch.dict(os.environ,{'TRVELLE_SEARCH_MODE':'replay','TRVELLE_REPLAY_DIR':directory,'TAVILY_API_KEY':''}):
    result=self.gateway.request('tavily',params)
    self.assertTrue(result['_trvelle_search']['stale'])
    with self.assertRaises(SearchBudgetError):self.gateway.request('tavily',{'query':'missing'})
  self.assertEqual(self.calls,[])
 def test_brave_places_are_cached_and_share_one_provider_budget(self):
  self.gateway.transport=Mock(return_value={'results':[{'title':'Museum','thumbnail':{'src':'https://example.test/photo.jpg'}}]})
  with search_context(limits={'brave':1}):
   result=self.gateway.request('brave',{'endpoint':'places','q':'Museum','location':'Rome Italy'})
   cached=self.gateway.request('brave',{'endpoint':'places','q':'Museum','location':'Rome Italy'})
   with self.assertRaises(SearchBudgetError):self.gateway.request('brave',{'endpoint':'pois','ids':['temporary-id']})
  self.assertEqual(cached['_trvelle_search']['mode'],'cache')
  self.assertEqual(result['results'][0]['thumbnail']['src'],'https://example.test/photo.jpg')
  self.gateway.transport.assert_called_once()
  self.assertLess(ttl('brave',{'endpoint':'places'}),8*3600)
  self.assertNotEqual(query_key('brave',{'endpoint':'places','q':'Museum','location':'Rome'}),query_key('brave',{'endpoint':'places','q':'Museum','location':'Paris'}))
 def test_web_research_cannot_consume_reserved_photo_requests(self):
  with search_context(limits={'brave':4},reserves={'brave_places':2}):
   self.gateway.request('brave',{'endpoint':'web','q':'first research'})
   self.gateway.request('brave',{'endpoint':'web','q':'second research'})
   with self.assertRaises(SearchBudgetError):self.gateway.request('brave',{'endpoint':'web','q':'third research'})
   self.gateway.request('brave',{'endpoint':'places','q':'Museum','location':'Rome'})
   self.gateway.request('brave',{'endpoint':'pois','ids':['matched-id']})
   self.assertEqual(self.gateway.request('brave',{'endpoint':'web','q':'first research'})['_trvelle_search']['mode'],'cache')
   with self.assertRaises(SearchBudgetError):self.gateway.request('brave',{'endpoint':'places','q':'Another museum','location':'Rome'})
  self.assertEqual(len(self.calls),4)
 def test_usage_shows_current_credentials_after_rotation(self):
  self.gateway.request('brave',{'q':'first'})
  self.assertEqual(len(self.gateway.status()['providers']),1)
  with patch.dict(os.environ,{'BRAVE_API_KEY':'rotated-test-key'}):
   self.assertEqual(self.gateway.status()['providers'],[])
   self.assertEqual(next(row for row in self.gateway.status()['requests'] if row['provider']=='brave')['completed'],1)
 def test_brave_unlimited_window_is_not_treated_as_exhausted(self):
  self.gateway.request('brave',{'q':'first'})
  reset=self.gateway.brave_headers(self.scope,{'x-ratelimit-limit':'50, 0','x-ratelimit-remaining':'49, 0','x-ratelimit-reset':'1, 2000000'},200)
  self.assertIsNone(reset)
  self.gateway.request('brave',{'q':'second'})
  self.assertEqual(len(self.calls),2)
 def test_brave_reset_blocks_live_queries_but_keeps_cached_results(self):
  self.gateway.request('brave',{'q':'first'})
  retry=self.gateway.brave_headers(self.scope,{'x-ratelimit-limit':'1, 100','x-ratelimit-remaining':'0, 0','x-ratelimit-reset':'1, 3600'},429)
  self.assertGreater((retry-datetime.now(timezone.utc)).total_seconds(),3500)
  self.assertEqual(self.gateway.request('brave',{'q':'first'})['_trvelle_search']['mode'],'cache')
  with self.assertRaises(SearchBudgetError):self.gateway.request('brave',{'q':'blocked'})
  self.assertEqual(len(self.calls),1)
 def test_known_failed_brave_request_releases_billing_and_prevents_immediate_retry(self):
  def failed(*args):
   retry=self.gateway.brave_headers(self.scope,{'retry-after':'60'},429)
   raise BraveResponseError(429,retry)
  self.gateway.transport=Mock(side_effect=failed)
  with search_context(limits={'brave':1}):
   for _ in range(2):
    with self.assertRaises(SearchBudgetError):self.gateway.request('brave',{'q':'rate limited'})
  self.gateway.transport.assert_called_once()
  with DBHandler.db_session() as db:
   charge=db.query(SearchCharge).filter_by(scope=self.scope).one()
   self.assertEqual((charge.status,charge.credits),('failed',0))
   self.assertEqual(db.query(SearchAccount).filter_by(scope=self.scope).one().used,50)
