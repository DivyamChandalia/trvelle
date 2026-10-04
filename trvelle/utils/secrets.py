"""Remove credentials from provider payloads before caching or exposing them."""
import re
import logging
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

SECRET_FIELDS = {'api_key', 'apikey', 'authorization', 'access_token', 'secret', 'password', 'serpapi_api_key', 'google_api_key', 'tavily_api_key', 'openrouter_api_key', 'brave_api_key', 'x-subscription-token', 'x_subscription_token'}

class CredentialLogFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r'(?i)([?&](?:api_key|apikey|access_token|password)=)[^&\s"\']+', r'\1[redacted]', record.getMessage())
        record.args = ()
        return True

http_logger = logging.getLogger('httpx')
http_logger.setLevel(logging.WARNING)
http_logger.addFilter(CredentialLogFilter())

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
