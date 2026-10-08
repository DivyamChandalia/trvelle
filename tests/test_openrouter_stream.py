import json,unittest,httpx
from trvelle.orchestrator.openrouter_stream import read_openrouter_stream
from trvelle.orchestrator.model_router import ProviderError

def response(chunks,done=True):
 body=': OPENROUTER PROCESSING\n\n'+''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)
 if done:body+='data: [DONE]\n\n'
 return httpx.Response(200,headers={'content-type':'text/event-stream'},content=body)

class StreamTests(unittest.IsolatedAsyncioTestCase):
 async def test_comments_fragmented_calls_and_usage_are_assembled(self):
  chunks=[{'id':'gen-fixture','model':'actual/free-model','choices':[{'delta':{'content':'Hello ','tool_calls':[{'index':0,'id':'call-fixture','function':{'name':'hotel_search','arguments':'{"q":'}}]}}]},
          {'choices':[{'delta':{'content':'world','tool_calls':[{'index':0,'function':{'arguments':'"Rome"}'}}]},'finish_reason':'tool_calls'}]},
          {'choices':[],'usage':{'completion_tokens':10}}]
  result=await read_openrouter_stream(response(chunks))
  self.assertEqual(result.content,'Hello world');self.assertEqual(result.tool_calls[0]['args'],{'q':'Rome'})
  self.assertEqual(result.additional_kwargs['served_model'],'actual/free-model')
  self.assertEqual(result.additional_kwargs['usage']['completion_tokens'],10)
 async def test_midstream_errors_preserve_real_retry_hint(self):
  with self.assertRaises(ProviderError) as caught:
   await read_openrouter_stream(response([{'error':{'code':429,'message':'Limited'}}]))
  self.assertEqual(caught.exception.status,429)
 async def test_truncated_response_is_not_published_as_complete(self):
  with self.assertRaises(httpx.RemoteProtocolError):
   await read_openrouter_stream(response([{'choices':[{'delta':{'content':'Partial'}}]}],False))
 async def test_reasoning_fragments_are_preserved_for_followup_tool_turns(self):
  chunks=[{'choices':[{'delta':{'reasoning_details':[{'type':'reasoning.text','index':0,'text':'First '}]}}]},
          {'choices':[{'delta':{'content':'Answer','reasoning_details':[{'type':'reasoning.text','index':0,'text':'second'}]},'finish_reason':'stop'}]}]
  result=await read_openrouter_stream(response(chunks))
  self.assertEqual(result.additional_kwargs['reasoning_details'][0]['text'],'First second')

 async def test_json_errors_before_stream_keep_retry_headers(self):
  with self.assertRaises(ProviderError) as caught:
   await read_openrouter_stream(httpx.Response(200,json={'error':{'code':429}},headers={'Retry-After':'120'}))
  self.assertEqual(caught.exception.status,429)
  self.assertEqual(caught.exception.headers['Retry-After'],'120')
