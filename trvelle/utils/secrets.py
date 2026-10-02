"""Remove credentials from provider payloads before caching or exposing them."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

SECRET_FIELDS = {'api_key', 'apikey', 'authorization', 'access_token', 'secret', 'password', 'serpapi_api_key', 'google_api_key', 'tavily_api_key', 'openrouter_api_key'}

def redact_secrets(value):
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items() if key.lower() not in SECRET_FIELDS}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and value.startswith(('http://', 'https://')):
        try:
            parts = urlsplit(value)
            query = [(k,v) for k,v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SECRET_FIELDS]
            return urlunsplit(parts._replace(query=urlencode(query)))
        except ValueError:
            return value
    return value
