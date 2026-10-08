"""Assemble OpenRouter SSE without treating keep-alive comments as JSON."""
import json
from copy import deepcopy
import httpx
from langchain_core.messages import AIMessage

async def read_openrouter_stream(response):
    from .model_router import ProviderError
    if 'application/json' in response.headers.get('content-type',''):
        await response.aread()
        body=response.json()
        if body.get('error'):
            code=body['error'].get('code',503)
            raise ProviderError(code if isinstance(code,int) else 503,body,response.headers)
        raise httpx.RemoteProtocolError('Expected an OpenRouter event stream')
    text, calls, reasoning, frames = [], {}, [], []
    identifier, served_model, usage, terminal, done = None, None, None, False, False
    def consume(data):
        nonlocal identifier, served_model, usage, terminal, done
        if data == '[DONE]':
            terminal, done = True, True
            return
        chunk = json.loads(data)
        if chunk.get('error'):
            code = chunk['error'].get('code',503)
            raise ProviderError(code if isinstance(code,int) else 503,chunk,response.headers)
        identifier = chunk.get('id') or identifier
        served_model = chunk.get('model') or served_model
        usage = chunk.get('usage') or usage
        for choice in chunk.get('choices') or []:
            if choice.get('index',0) != 0:continue
            if choice.get('finish_reason') == 'error':raise httpx.RemoteProtocolError('OpenRouter generation was interrupted')
            if choice.get('finish_reason') is not None:terminal = True
            delta = choice.get('delta') or {}
            if isinstance(delta.get('content'),str):text.append(delta['content'])
            for detail in delta.get('reasoning_details') or []:
                if not isinstance(detail,dict):continue
                match = next((old for old in reasoning if old.get('type')==detail.get('type') and old.get('index')==detail.get('index') and old.get('id')==detail.get('id')),None)
                if match is not None and detail.get('type') in ('reasoning.text','reasoning.summary'):
                    for field,value in detail.items():
                        if field in ('text','summary') and isinstance(value,str):match[field]=match.get(field,'')+value
                        else:match[field]=value
                elif detail not in reasoning:reasoning.append(deepcopy(detail))
            for entry in delta.get('tool_calls') or []:
                item=calls.setdefault(entry.get('index',0),{'id':'','name':'','arguments':''})
                for field,value in [('id',entry.get('id')),('name',(entry.get('function') or {}).get('name'))]:
                    if value and value != item[field]:item[field]+=value
                item['arguments']+=(entry.get('function') or {}).get('arguments') or ''
    async for line in response.aiter_lines():
        if line.startswith(':'):continue
        if not line:
            if frames:consume('\n'.join(frames));frames.clear()
            if done:break
            continue
        if line.startswith('data:'):frames.append(line[5:].lstrip(' '))
    if frames:consume('\n'.join(frames))
    if not terminal:raise httpx.RemoteProtocolError('OpenRouter response ended before completion')
    tool_calls=[]
    for item in calls.values():
        arguments=json.loads(item['arguments'])
        if not isinstance(arguments,dict) or not item['id'] or not item['name']:raise ValueError('Invalid model tool arguments')
        tool_calls.append({'id':item['id'],'name':item['name'],'args':arguments})
    return AIMessage(content=''.join(text),tool_calls=tool_calls,id=identifier,
                     additional_kwargs={'reasoning_details':reasoning,'served_model':served_model,'usage':usage})
