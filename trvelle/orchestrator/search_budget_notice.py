"""Budget exhaustion is a planning constraint, not an agent failure."""
import json

SEARCH_TOOLS={'flight_search','hotel_search','tavily_search','web_search','brave_place_search'}

def exhausted_providers(snapshot):
    usage,limits=snapshot.get('search_usage',{}),snapshot.get('search_limits',{})
    return {name for name,limit in limits.items() if isinstance(limit,(int,float)) and usage.get(name,0)>=limit}

def blocked_search_tools(snapshot):
    exhausted=exhausted_providers(snapshot)
    blocked=set()
    if 'serpapi' in exhausted:blocked.update(('flight_search','hotel_search'))
    if 'brave' in exhausted:blocked.add('brave_place_search')
    # Web searches may fall back between Brave and Tavily; keep them while either has budget.
    if {'brave','tavily'}<=exhausted:blocked.update(('web_search','tavily_search'))
    return blocked

def budget_notice(providers):
    return json.dumps({'status':'budget_exhausted','providers':sorted(providers),
        'instruction':'The search budget is exhausted. Do not repeat these searches or request additional paid API calls. Work with the saved search results and existing itinerary. Complete the best supported plan you can; clearly mark missing details and unpriced items. If inventory is missing, publish a partial draft with unfinished fields rather than inventing flights, hotels, availability or quotes. Only an explicit user budget extension permits more searches.'})

def budget_exception(error):
    from trvelle.tools.search_gateway import SearchBudgetError
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,SearchBudgetError) and any(word in str(error).lower() for word in ('budget','quota','credit','limit')):return error
        error=error.__cause__ or error.__context__
    return None
