"""Bound research work and model context without changing saved evidence."""
from langchain_core.messages import AIMessage, ToolMessage
import json
import re

LIMITS = {"hotel_search": 3, "tavily_search": 6}


def hotel_offer_catalog(content):
    """Keep each property's identity attached to its own quote when shortening evidence."""
    blocks = re.split(r'(?=\*\*Hotel \d+:)', content)
    offers = []
    for block in blocks:
        if not block.startswith('**Hotel '):
            continue
        lines = block.splitlines()
        keep = [line for line in lines if line.startswith(('**Hotel ', 'The UID ', 'Rating:',
                'Location Rating:', 'Rate per night:', 'Total rate:', 'Check-in:', 'Hotel Class:'))]
        if any(line.startswith('The UID ') for line in keep):
            offers.append('\n'.join(keep))
    header = next((line for line in content.splitlines() if line.startswith('Stay:')), '')
    return (header + '\n' if header and offers else '') + '\n\n'.join(offers)


def counts(history):
    return {name: sum(isinstance(message, ToolMessage) and message.name in
        (('tavily_search','web_search','brave_place_search') if name=='tavily_search' else (name,)) for message in history) for name in LIMITS}


def should_finalize(history):
    used = counts(history)
    rounds = sum(isinstance(message, AIMessage) for message in history)
    return rounds >= 4 or all(used[name] >= maximum for name, maximum in LIMITS.items())


def compact(history):
    """Keep call/result pairing and original DB records; shorten prompt copies."""
    recent = [index for index, message in enumerate(history) if isinstance(message, ToolMessage)][-3:]
    result = []
    for index, message in enumerate(history):
        if isinstance(message, ToolMessage) and isinstance(message.content, str):
            limit = (5000 if message.name == "hotel_search" else 2500) if index in recent else 700
            if len(message.content) > limit:
                if message.name == 'hotel_search' and (catalog := hotel_offer_catalog(message.content)):
                    result.append(message.model_copy(update={'content': catalog + '\nOther property details remain saved. Do not repeat this search.'}))
                    continue
                try:
                    parsed = json.loads(message.content)
                    if isinstance(parsed, dict) and isinstance(parsed.get('results'), list):
                        parsed['results'] = [{'title': item.get('title'), 'url': item.get('url'), 'content': item.get('content', '')[:max(200, limit // max(1, len(parsed['results'])))]} for item in parsed['results']]
                        text = json.dumps(parsed, ensure_ascii=False)
                    else:
                        raise ValueError()
                except (ValueError, TypeError):
                    uids = re.findall(r'UID[^\n]*["\']([^"\']+)["\']', message.content)
                    text = message.content[:limit] + ('\nSaved offer identifiers: ' + ', '.join(uids) if uids else '')
                text += "\n[Additional saved search details omitted from this prompt. Use the verified options shown; do not repeat the search.]"
                message = message.model_copy(update={"content": text})
        result.append(message)
    return result
