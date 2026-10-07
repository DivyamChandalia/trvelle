import hashlib,json,os,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import httpx
from trvelle.database import DBHandler
from trvelle.database.models import SearchCache,SearchAccount,SearchCharge,SearchProviderState
from trvelle.tools.affiliate_links import AffiliateLinks,eligible_url,retry_time

class AffiliateTests(unittest.TestCase):
 def setUp(self):
  self.token=uuid.uuid4().hex;self.marker=str(uuid.uuid4().int % 1000000000 + 1)
  self.env=patch.dict(os.environ,{'TRAVELPAYOUTS_API_TOKEN':self.token,'TRAVELPAYOUTS_PROJECT_ID':'12345','TRAVELPAYOUTS_MARKER':self.marker,'TRAVELPAYOUTS_REQUESTS_PER_MINUTE':'80'})
  self.env.start();self.calls=[];self.http_status=200;self.mode='success'
  def send(request):
   self.calls.append(request);body=json.loads(request.content);url=body['links'][0]['url']
   if self.mode=='timeout':raise httpx.ReadTimeout('fixture timeout',request=request)
   item={'url':url,'code':'success','partner_url':'https://booking.tp.st/test-affiliate-fixture'}
   if self.mode=='unapproved':item={'url':url,'code':'failed','message':'trs is not subscribed for brand','partner_url':''}
   if self.mode=='unsafe':item['partner_url']='javascript:alert(1)'
   return httpx.Response(self.http_status,json={'code':'success','result':{'links':[item]}},headers={'Retry-After':'120'})
  self.links=AffiliateLinks(DBHandler.db_session,DBHandler.engine,httpx.MockTransport(send))
  self.url='https://www.booking.com/hotel/it/test.html?checkin=2027-03-14&checkout=2027-03-17&group_adults=2'
 def tearDown(self):
  scope=hashlib.sha256(f'{self.token}:12345:{self.marker}'.encode()).hexdigest()
  rate=hashlib.sha256(f'travelpayouts:{self.marker}'.encode()).hexdigest()
  with DBHandler.db_session() as db:
   for model in (SearchCache,SearchCharge,SearchProviderState,SearchAccount):db.query(model).filter(model.scope.in_([scope,rate])).delete()
   db.commit()
  self.env.stop()
 def test_coalesced_cached_links_preserve_the_exact_destination_and_hide_token(self):
  with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.links.convert(self.url),range(2)))
  self.assertEqual(results,['https://booking.tp.st/test-affiliate-fixture']*2);self.assertEqual(len(self.calls),1)
  request=self.calls[0];body=json.loads(request.content)
  self.assertEqual(body['links'],[{'url':self.url,'sub_id':'hotel'}]);self.assertEqual(request.headers['X-Access-Token'],self.token)
  with DBHandler.db_session() as db:
   caches=db.query(SearchCache).filter_by(provider='travelpayouts').all()
   self.assertFalse(any(self.token in json.dumps(c.query) or self.token in json.dumps(c.result) for c in caches))
 def test_unapproved_brand_is_negative_cached_and_returns_original(self):
  self.mode='unapproved'
  for _ in range(2):self.assertEqual(self.links.convert(self.url),self.url)
  self.assertEqual(len(self.calls),1)
 def test_unsupported_google_post_and_lookalike_domains_never_call_the_api(self):
  for url in ['https://www.google.com/travel/clk/booking','https://www.booking.com.evil.test/hotel','https://127.0.0.1/','https://airline.test/confirm']:
   self.assertFalse(eligible_url(url));self.assertEqual(self.links.convert(url),url)
  self.assertEqual(self.links.convert(self.url,post_data='flight=fixture'),self.url)
  self.assertEqual(self.calls,[])
 def test_cooldown_skips_new_links_and_honors_provider_retry_after(self):
  self.http_status=429;self.assertEqual(self.links.convert(self.url),self.url)
  second=self.url+'&room=2';self.assertEqual(self.links.convert(second),second)
  self.assertEqual(len(self.calls),1)
  rate=hashlib.sha256(f'travelpayouts:{self.marker}'.encode()).hexdigest()
  with DBHandler.db_session() as db:self.assertGreater((db.get(SearchProviderState,rate).cooldown_until-datetime.now(timezone.utc)).total_seconds(),110)
 def test_local_rate_limit_and_missing_configuration_preserve_booking(self):
  with patch.dict(os.environ,{'TRAVELPAYOUTS_REQUESTS_PER_MINUTE':'1'}):
   self.links.convert(self.url)
   second=self.url+'&room=3';self.assertEqual(self.links.convert(second),second)
  with patch.dict(os.environ,{'TRAVELPAYOUTS_API_TOKEN':''}):self.assertEqual(self.links.convert(self.url+'&room=4'),self.url+'&room=4')
  self.assertEqual(len(self.calls),1)
 def test_network_failure_and_invalid_affiliate_destination_return_original(self):
  self.mode='unsafe';self.assertEqual(self.links.convert(self.url),self.url)
  self.mode='timeout';other=self.url+'&room=5';self.assertEqual(self.links.convert(other),other)
 def test_retry_date_is_understood(self):
  now=datetime.now(timezone.utc);target=now+timedelta(minutes=10)
  self.assertGreater((retry_time(target.strftime('%a, %d %b %Y %H:%M:%S GMT'),now)-now).total_seconds(),590)
