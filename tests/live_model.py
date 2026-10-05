"""Explicit, bounded live model check using historical search fixtures.

Run only when live inference is authorized. Uses the chosen existing account,
never calls a search provider, never writes a chat to the application database.
Results are written to an ignored local runtime artifact for inspection.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from trvelle.orchestrator.personal_models import get_accounts
from trvelle.tools.itinerary_tool import Itinerary
from trvelle.utils.itinerary_validation import validate_trip

class Submission(BaseModel):
    itinerary: Itinerary

async def submit(itinerary):
    return 'Validated'

async def main(args):
    root = Path(__file__).resolve().parent
    evidence = json.loads((root / 'fixtures/acceptance/singapore-itinerary.json').read_text())
    evidence['summary']['budget_amount'] = 250000
    evidence['summary']['budget'] = 'INR 250000 for two adults. Historical quotes are evidence only, not current availability.'
    # Match production enrichment: the model chooses UIDs; metadata remains in storage.
    compact = {key: evidence[key] for key in ('summary', 'requirements') if key in evidence}
    compact['flights'] = evidence['travel_options']['flights']
    compact['hotels'] = [{key: hotel.get(key) for key in ('choose_uid','name','selected','check_in_date','check_out_date','currency','total_rate','prices','location','essential_info')}
                         for hotel in evidence['travel_options']['hotels'] if hotel.get('selected')]
    compact['suggested_places'] = [{ 'day': day['day'], 'activities': [{'title': item.get('title'), 'location': item.get('location'), 'visitor_info':item.get('visitor_info')} for item in day['items'] if item['card_type']=='activity']}
                                 for day in evidence['daily_plan']]
    accounts = get_accounts()
    owner = str(uuid.UUID(args.owner))
    result = {'live_model_calls': 0, 'search_requests': 0, 'source': 'Historical fixtures recorded October 4, 2026'}
    try:
        began = time.monotonic()
        # Research with the same evidence already retrieved by the real travel tools.
        research = await asyncio.wait_for(accounts.request(owner, {'provider':'chatgpt','model':args.researcher,'effort':'medium'}, [
            SystemMessage(content='You are a travel researcher. Review the supplied historical evidence, dates, lodging coverage, flight times, pacing and budget. In 350 words or less give the planner actionable corrections. Do not invent quotes, images or sources. No additional searches are authorized.'),
            HumanMessage(content=json.dumps(compact))], []), 240)
        result['live_model_calls'] += 1
        result['researcher_seconds'] = round(time.monotonic()-began, 1)
        checkpoint = Path('.runtime/live-model-checkpoint.json')
        checkpoint.parent.mkdir(exist_ok=True); checkpoint.write_text(json.dumps({'researcher_seconds':result['researcher_seconds'],'review':research.text})); checkpoint.chmod(0o600)
        print('Research review completed; compact evidence characters:', len(json.dumps(compact)), '; search requests: 0.', flush=True)
        tool = StructuredTool.from_function(coroutine=submit, name='publish_itinerary', description='Submit the full validated itinerary.', args_schema=Submission)
        began = time.monotonic()
        answer = await asyncio.wait_for(accounts.request(owner, {'provider':'chatgpt','model':args.orchestrator,'effort':'medium'}, [
            SystemMessage(content='You are the travel planner. Publish a complete five-day Bengaluru–Singapore itinerary for two adults, November 15–19, 2026, by calling publish_itinerary. Use the supplied historical flight and hotel evidence and their unchanged UIDs, prices and dates. Use the selected hotel UID for every overnight stay and the selected flight UID for the flights. Output only summary and daily_plan plus concise budget allowances. Saved offer metadata, photos and prices are attached by the application, so do not copy them into your output. Keep all display currency INR. Budget INR 250000. Include realistic airport connections, suggested local sightseeing times, named locations, four overnight hotel stays and at most two main attractions per full day. Keep descriptions concise. Mark all quotes historical and baggage or room types unverified where needed. Do not claim current availability or fabricate photos or visitor details. No further searches are authorized.'),
            HumanMessage(content=json.dumps(compact)),
            HumanMessage(content='Research review: '+research.text)], [tool]), 240)
        result['live_model_calls'] += 1
        result['orchestrator_seconds'] = round(time.monotonic()-began,1)
        calls = [call for call in answer.tool_calls if call['name']=='publish_itinerary']
        if not calls: raise RuntimeError('Planner did not submit the itinerary schema')
        plan = Submission.model_validate(calls[0]['args']).itinerary.model_dump(mode='json')
        plan['travel_options'] = evidence['travel_options']
        plan['selected_flight_uid'] = evidence['selected_flight_uid']
        report = validate_trip(plan)
        result.update({'orchestrator':args.orchestrator,'researcher':args.researcher,'days':len(plan['daily_plan']),
                       'validation':report,'itinerary':plan,'research_review':research.text})
        assert len(plan['daily_plan']) == 5
        activities = [item for day in plan['daily_plan'] for item in day['items'] if item['card_type']=='activity']
        assert len(activities) >= 6 and all(item.get('start_time') and item.get('location') for item in activities)
        assert report['priced_total'] == 173988, 'Flight/hotel quote changed'
        output = Path('.runtime/acceptance-live-model.json')
        output.parent.mkdir(exist_ok=True); output.write_text(json.dumps(result,indent=2)); output.chmod(0o600)
        print(json.dumps({key:result[key] for key in ('live_model_calls','search_requests','orchestrator','researcher','days','researcher_seconds','orchestrator_seconds')}),flush=True)
        print('Structured plan validated. Historical flight/stay total INR 173988 retained.',flush=True)
    finally:
        await accounts.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner',required=True)
    parser.add_argument('--orchestrator',default='gpt-6.1-sol')
    parser.add_argument('--researcher',default='gpt-6-luna')
    asyncio.run(main(parser.parse_args()))
