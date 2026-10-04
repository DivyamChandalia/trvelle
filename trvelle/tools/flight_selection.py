"""Select a saved flight and re-query any dependent return/multi-city legs."""
from copy import deepcopy
from .flight_search import searcher
from ..utils.secrets import redact_secrets

def flight_options(part):
    return (part.get('best_flights') or []) + (part.get('other_flights') or [])

def choose_flight(raw, search_index, option_index):
    parts = [deepcopy(part) for part in raw if isinstance(part, dict) and flight_options(part)]
    if search_index >= len(parts) or option_index >= len(flight_options(parts[search_index])):
        raise ValueError('This flight option is no longer available. Reopen flight details.')
    result = parts[:search_index+1]
    part = result[-1]
    part['selected_option_index'] = option_index
    chosen = flight_options(part)[option_index]
    params = deepcopy(part.get('search_parameters') or {})
    params['api_key'] = searcher.api_key
    params['engine'] = 'google_flights'
    # Return prices/availability depend on the selected outbound flight token.
    seen = set()
    while chosen.get('departure_token'):
        token = chosen['departure_token']
        if token in seen or len(result) >= 8:
            raise ValueError('Unable to resolve the remaining flight legs. Try another option.')
        seen.add(token)
        params['departure_token'] = token
        following = searcher._fetch_results(params, 'cache/selected-flight.json')
        if not flight_options(following):
            raise ValueError('No connecting or return options remain for this flight. Try another option.')
        following['selected_option_index'] = 0
        result.append(following)
        chosen = flight_options(following)[0]
    if len(parts) > len(result):
        raise ValueError('The provider did not return the remaining flight legs. Try another option.')
    uid = next((part['choose_uid'] for part in raw if isinstance(part, dict) and part.get('choose_uid')), None)
    result.append({'choose_uid': uid})
    return redact_secrets(result)
