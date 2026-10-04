"""Bounded, repeatable Brave/Tavily comparison; uses the shared cache/spend boundary."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from trvelle.utils import load_environment
load_environment()
from trvelle.tools.search_gateway import gateway, search_context, SearchBudgetError
from trvelle.tools.web_search import research_search
from trvelle.tools.place_search import find_place, tokens

async def compare(args):
    queries=json.loads(Path(args.queries).read_text())
    if len(queries)>4: raise ValueError('The live comparison is limited to four query pairs.')
    report={'created_at':datetime.now(timezone.utc).isoformat(), 'defaults_changed':False,
        'method':'Identical queries, five results, basic Tavily search, Brave extra snippets; no LLM evaluation. Relevance uses name/location tokens and official-domain coverage. Place matching is a separate capability check, not a web-search score. Tavily images are not requested by the current adapter; thumbnail counts do not compare image-search capability. Cached timings do not establish live-provider speed.',
        'limits':{'serpapi':0,'tavily':4,'brave':12},'queries':[]}
    with search_context(limits=report['limits']):
        for case in queries:
            row={**case,'providers':{}}
            for provider in ('brave','tavily'):
                start=time.perf_counter()
                try:
                    data=await research_search(case['query'],5,provider=provider,fallback=False)
                    results=data.get('results') or []
                    name=set(tokens(case['name']));location=set(tokens(case['location']))
                    evaluated=[]
                    for r in results:
                        text=set(tokens(r.get('title','')+' '+r.get('content','')))
                        domain=(urlparse(r.get('url') or '').hostname or '').removeprefix('www.')
                        evaluated.append({**r,'name_match':bool(name & text),'location_match':bool(location & text),
                            'official':any(domain==d or domain.endswith('.'+d) for d in case['official_domains'])})
                    row['providers'][provider]={'status':'ok','latency_ms':round((time.perf_counter()-start)*1000),
                        'mode':(data.get('_trvelle_search') or {}).get('mode'), 'result_count':len(evaluated),
                        'relevant_results':sum(r['name_match'] and r['location_match'] for r in evaluated),
                        'official_results':sum(r['official'] for r in evaluated),
                        'thumbnail_results':sum(bool(r.get('thumbnail')) for r in evaluated),'results':evaluated}
                except (SearchBudgetError,RuntimeError) as error:
                    row['providers'][provider]={'status':'unavailable','error':str(error),'latency_ms':round((time.perf_counter()-start)*1000)}
            start=time.perf_counter()
            try:
                place=await find_place(case['name'],case['location'],photos=True,source_url='https://'+case['official_domains'][0])
                row['brave_place']={'status':'matched' if place else 'no confident match','latency_ms':round((time.perf_counter()-start)*1000),'place':place}
            except SearchBudgetError as error:
                row['brave_place']={'status':'unavailable','error':str(error)}
            report['queries'].append(row)
    report['summary']={provider:{
        'successful_queries':sum(row['providers'][provider]['status']=='ok' for row in report['queries']),
        'relevant_results':sum(row['providers'][provider].get('relevant_results',0) for row in report['queries']),
        'official_results':sum(row['providers'][provider].get('official_results',0) for row in report['queries']),
        'thumbnail_results':sum(row['providers'][provider].get('thumbnail_results',0) for row in report['queries']),
        'mean_latency_ms':round(sum(row['providers'][provider]['latency_ms'] for row in report['queries'])/max(1,len(report['queries'])))
    } for provider in ('brave','tavily')}
    report['summary']['places']={'matched':sum(r['brave_place']['status']=='matched' for r in report['queries']),
        'with_photos':sum(bool((r['brave_place'].get('place') or {}).get('photos')) for r in report['queries'])}
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    lines=['# Brave and Tavily travel comparison','',report['method'],'',
           'Defaults were retained. This small sample measures useful result coverage, not factual correctness or future-date availability. Cached and live timings are labelled separately.','',
           '| Query | Brave relevant / official | Tavily relevant / official | Brave place |','|---|---|---|---|']
    for r in report['queries']:
        cells=[]
        for p in ('brave','tavily'):
            entry=r['providers'][p];cells.append(f"{entry.get('relevant_results',0)} / {entry.get('official_results',0)} ({entry.get('mode') or entry['status']})")
        place=r['brave_place'];photo=' · photo' if (place.get('place') or {}).get('photos') else ''
        lines.append(f"| {r['name']} — {r['location']} | {' | '.join(cells)} | {place['status']}{photo} |")
    lines += ['', '## Results for review', '']
    for r in report['queries']:
        lines += [f"### {r['name']}",'']
        for p in ('brave','tavily'):
            lines += [f"**{p.title()}**",'']
            for result in r['providers'][p].get('results',[]):
                lines.append(f"- [{result.get('title') or 'Result'}]({result.get('url')}) — {result.get('content','')[:450]}")
            if r['providers'][p].get('error'):lines.append(r['providers'][p]['error'])
            lines.append('')
    path.with_suffix('.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'report':str(path),'summary':report['summary'],'defaults_changed':False},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Explicitly authorize up to four Tavily credits and twelve Brave requests (cache hits are free).')
    parser.add_argument('--queries',default=str(ROOT/'tests/fixtures/search-comparison-queries.json'))
    parser.add_argument('--output',default=str(ROOT/'.runtime/reports/brave-vs-tavily.json'))
    args=parser.parse_args()
    if not args.live:
        raise SystemExit('Use --live for the bounded comparison, or set TRVELLE_SEARCH_MODE=replay with matching fixtures and --live to execute without network.')
    asyncio.run(compare(args))
