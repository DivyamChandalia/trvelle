import unittest
from trvelle.utils.secrets import redact_secrets
class SecretTests(unittest.TestCase):
 def test_nested_fields_and_links_are_cleaned_without_removing_images(self):
  raw={'search_parameters':{'api_key':'secret','q':'Venice'},'properties':[{'thumbnail':'https://img.example/hotel.jpg','link':'https://example/search?api_key=secret&q=Venice'}]}
  safe=redact_secrets(raw)
  self.assertNotIn('api_key',safe['search_parameters'])
  self.assertEqual(safe['properties'][0]['thumbnail'],raw['properties'][0]['thumbnail'])
  self.assertEqual(safe['properties'][0]['link'],'https://example/search?q=Venice')
  self.assertEqual(raw['search_parameters']['api_key'],'secret')
 def test_formatted_http_logs_redact_query_credentials(self):
  import logging
  from trvelle.utils.secrets import CredentialLogFilter
  record=logging.LogRecord('httpx',logging.INFO,'test',1,'HTTP Request: %s %s',('GET','https://serpapi.com/account.json?api_key=private-test-secret&q=Singapore'),None)
  CredentialLogFilter().filter(record)
  self.assertNotIn('private-test-secret',record.getMessage())
  self.assertIn('q=Singapore',record.getMessage())
