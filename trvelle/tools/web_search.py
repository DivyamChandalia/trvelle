"""Small async Tavily adapter with explicit HTTP error handling."""
import json
import os
import httpx
from .search_gateway import gateway, active_search, SearchBudgetError
from langchain_core.tools import tool

@tool
async def tavily_search(query: str, max_results: int = 3, include_domains: list[str] | None = None) -> dict:
    """Research destination activities and practical travel information. Optionally restrict to known source domains (hostnames such as uffizi.it)."""
    params = {'query': query, 'max_results': max(1, min(max_results, 5)), 'search_depth': 'basic'}
    if include_domains:
        params['include_domains'] = list(dict.fromkeys(domain.strip().casefold().removeprefix('www.') for domain in include_domains if domain.strip()))[:10]
    data = await gateway.arequest('tavily', params)
    return {"result": json.dumps(data, ensure_ascii=False), 'raw': data}


def search_providers():
    context = active_search.get()
    choices = context.providers if context else {}
    return {'web':choices.get('web') or os.getenv('TRVELLE_WEB_SEARCH_PROVIDER') or 'brave',
            'places':choices.get('places') or os.getenv('TRVELLE_PLACE_SEARCH_PROVIDER') or 'brave'}


async def research_search(query, max_results=3, include_domains=None, provider=None, fallback=True):
    """Normalize web evidence for any researcher; explicit tests can disable fallback."""
    provider = provider or search_providers()['web']
    if provider not in ('brave', 'tavily'):
        raise ValueError('Choose Brave or Tavily for web research')
    if provider == 'tavily':
        response = await tavily_search.ainvoke({'query':query, 'max_results':max_results, 'include_domains':include_domains})
        return {**response['raw'], 'provider':'tavily'}
    domains = [d.strip().casefold().removeprefix('www.') for d in (include_domains or []) if d.strip()][:10]
    q = query + (' (' + ' OR '.join('site:' + d for d in domains) + ')' if domains else '')
    try:
        raw = await gateway.arequest('brave', {'endpoint':'web', 'q':q, 'count':max(1,min(max_results,5)), 'extra_snippets':True})
        results = [{'title':r.get('title',''), 'url':r.get('url',''),
                    'content':' '.join([r.get('description') or '', *(r.get('extra_snippets') or [])]).strip(),
                    **({'thumbnail':r['thumbnail']} if r.get('thumbnail') else {})} for r in (raw.get('web') or {}).get('results', [])]
        if not results and fallback and os.getenv('TAVILY_API_KEY'):
            data = await research_search(query, max_results, include_domains, 'tavily', False)
            return {**data, 'fallback_from':'brave', 'provider_notice':'Brave returned no web evidence; Tavily was used.'}
        return {'provider':'brave', 'results':results, 'locations':raw.get('locations'), '_trvelle_search':raw.get('_trvelle_search')}
    except (SearchBudgetError, httpx.HTTPError):
        if not fallback or not os.getenv('TAVILY_API_KEY'):
            raise
        data = await research_search(query, max_results, include_domains, 'tavily', False)
        return {**data, 'fallback_from':'brave', 'provider_notice':'Brave was unavailable; Tavily was used.'}


@tool
async def web_search(query: str, max_results: int = 3, include_domains: list[str] | None = None) -> dict:
    """Research activities, destinations, opening hours and travel information with the configured provider. Restrict to official source hostnames when known. Brave falls back to Tavily if unavailable."""
    data = await research_search(query, max_results, include_domains)
    return {'result':json.dumps(data,ensure_ascii=False), 'raw':data}
