"""Keep airline baggage research separate from the selected fare's inclusions."""
import html,re
from urllib.parse import urlsplit

AIRLINES = {
 'JL':('Japan Airlines','jal.co.jp'), 'NH':('ANA','ana.co.jp'), 'VJ':('Vietjet Air','vietjetair.com'),
 'LH':('Lufthansa','lufthansa.com'), 'AI':('Air India','airindia.com'), '6E':('IndiGo','goindigo.in'),
 'SQ':('Singapore Airlines','singaporeair.com'), 'TG':('Thai Airways','thaiairways.com'),
 'EK':('Emirates','emirates.com'), 'QR':('Qatar Airways','qatarairways.com'), 'EY':('Etihad Airways','etihad.com'),
 'AF':('Air France','airfrance.com'), 'KL':('KLM','klm.com'), 'BA':('British Airways','britishairways.com'),
 'GF':('Gulf Air','gulfair.com'), 'TK':('Turkish Airlines','turkishairlines.com'), 'CX':('Cathay Pacific','cathaypacific.com'),
}
ALIASES = {'jal':'JL','japanairlines':'JL','allnipponairways':'NH','ana':'NH','vietjet':'VJ','vietjetair':'VJ','indigo':'6E'}


def airline_scope(flight):
 name=flight.get('airline') or ''
 normalized=re.sub(r'[^a-z0-9]','',name.casefold())
 code=ALIASES.get(normalized) or next((code for code,(label,_) in AIRLINES.items() if re.sub(r'[^a-z0-9]','',label.casefold())==normalized),None)
 if not code:
  number=re.match(r'^([A-Z0-9]{2})\s*\d',str(flight.get('flight_number','')).upper())
  code=number.group(1) if number else None
 info=AIRLINES.get(code)
 return {'name':name or (info[0] if info else ''),'domains':[info[1]] if info else []}


def clean_evidence_text(text, limit=1800):
 text=re.sub(r'<[^>]*>',' ',html.unescape(str(text or '')))
 return ' '.join(text.split())[:limit]


def flight_policy_evidence(evidence, context):
 domains=context.get('official_domains') or []
 if not domains:return []
 result=[];seen=set()
 for entry in evidence:
  try:host=(urlsplit(entry.get('url','')).hostname or '').lower().removeprefix('www.')
  except ValueError:continue
  if not any(host==domain or host.endswith('.'+domain) for domain in domains):continue
  content=clean_evidence_text(entry.get('content'))
  title=clean_evidence_text(entry.get('title'),160)
  if not re.search(r'\bbaggage\b|\bluggage\b|carry.on|checked bag',title+' '+content,re.I):continue
  if entry['url'] in seen:continue
  seen.add(entry['url']);result.append({'url':entry['url'],'title':title,'content':content})
 return result
