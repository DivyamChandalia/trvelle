"""Route explicit trip actions separately from read-only conversation."""
import re


def message_mode(message, has_plan=False, continuing_plan=False):
    text=' '.join(message.casefold().split()).strip()
    if re.search(r'\b(?:i want|i need|let[\'’]?s)\b.{0,20}\b(?:new|another) (?:trip|itinerary)\b',text):
        return 'plan'
    request=text
    for _ in range(3):
        request=re.sub(r'^(?:please\s+|(?:can|could|would|will) you\s+(?:please\s+)?|(?:i want|i would like|i\'d like) you to\s+|let[\'’]?s\s+)', '',request)
    creation=r'^(?:plan|build|create|organize|design|help me (?:plan|organize))\b'
    if re.search(r'^(?:i (?:want|need|would like)|i\'d like) to (?:know|understand|ask|check)\b',text):
        return 'chat'
    if re.search(creation,request) or re.search(r'^(?:a |an )?(?:new|another) trip\b',request):
        return 'plan' if not has_plan or re.search(r'\b(?:new|another) (?:trip|itinerary)|\b\d+[- ]day\b|\b(?:trip|vacation|holiday)\b',request) else 'update'
    action=r'^(?:add|remove|replace|swap|change|update|edit|move|reschedule|adjust|set|reduce|increase|extend|shorten|rename|replan|complete|finish|recommend|suggest|find|search|look up|include|make|use|book)\b'
    if re.search(action,request):
        return 'update' if has_plan else 'plan'
    if re.search(r'^(?:i (?:want|need|would like)|i\'d like)\b',text):
        return 'update' if has_plan else 'plan'
    if re.search(r'^(?:how|what|why|when|where|which|who|is|are|does|do|did|will|would|should|can|could|tell me|explain|describe|summari[sz]e|remind me|compare)\b',request):
        return 'chat'
    if not has_plan and (continuing_plan or re.search(r'\b(?:trip|itinerary|vacation|holiday|travel|\d+[- ]day)\b',text)):
        return 'plan'
    return 'chat'
