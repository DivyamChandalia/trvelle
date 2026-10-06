"""Read-only answers reuse saved plan context; they cannot invoke planning tools."""
import json
from langchain_core.messages import AIMessage,HumanMessage,SystemMessage
from .run_store import store
from trvelle.database.chat_versions import ordered_active_messages


def recent_conversation(owner,chat_id,limit=12):
    with store.sessions() as db:
        rows=ordered_active_messages(db,owner,chat_id)
        messages=[{'role':row.type,'text':row.content[:3000]} for row in rows if row.type=='human' or (row.type=='ai' and row.message_name=='Supervisor_Agent' and row.content and not (row.additional_kwargs or {}).get('tool_calls'))]
        db.commit()
    return messages[-limit:]


async def answer_chat(agent,config):
    saved=store.operation(config['run_id'],'chat-answer','model')
    if saved:
        response=AIMessage.model_validate(saved)
    else:
        config['defer_plain_response']=True
        store.checkpoint(config['run_id'],'compose',task='Answering your question')
        prompt=[SystemMessage(content='You are Trvelle’s travel assistant in READ-ONLY conversation mode. Answer the user’s question naturally using the saved itinerary, its latest edit diff and conversation context. Questions about flights, hotels, costs, itinerary choices and remembered preferences do not request planning. Do not search, modify, publish or claim to update the itinerary. Preserve manual edits as authoritative. Use recorded flight durations and local times rather than inventing them. Distinguish source quotes, assumptions and missing details. If the user asks a general question, answer it directly. Avoid appending a generic itinerary-unchanged completion message.'),
            HumanMessage(content='Saved plan base:\n'+json.dumps(config.get('context_base') or {},sort_keys=True,ensure_ascii=False,default=str)),
            HumanMessage(content=json.dumps({'latest_edit_diff':config.get('context_diff') or [],'conversation':recent_conversation(config['user_id'],config['chat_id']),'question':config['query']},sort_keys=True,ensure_ascii=False,default=str))]
        response=await agent.invoke_model(config,'supervisor',[],prompt)
        if not response.text or response.tool_calls:
            raise ValueError('The assistant could not return a read-only answer. Try asking again.')
        store.operation(config['run_id'],'chat-answer','model',response.model_dump(mode='json'))
    config['final_response']=response
    return response.text
