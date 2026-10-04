from typing import Optional, List, AsyncGenerator, Any, Dict
import yaml
from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio
import ast
import re
from datetime import date
import os
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, ToolMessage, HumanMessage, AIMessage
from ..tools.web_search import tavily_search, web_search, search_providers
from ..tools.place_search import brave_place_search, enrich_itinerary
from ..utils import get_logger, load_environment, configure_logging
from ..prompts import SUPERVISOR_INSTRUCTIONS, RESEARCHER_INSTRUCTIONS
from ..database import DBHandler, InMemoryChatManager
from .tool_validator import tool_validator, ToolValidationError
import uuid
import json

load_environment()
configure_logging()
logger = get_logger(__name__)

class Orchestrator:
    db_handler = DBHandler()
    
    def __init__(self, chat_cleanup_interval_minutes: int = 5, chat_expiry_minutes: int = 10):
        from .model_router import ModelRouter
        self.model_router = ModelRouter()
        self.mcp_config = self.load_mcp_config()
        self.mcp_client = MultiServerMCPClient(self.mcp_config['mcp_servers'])
        self.tavily_search = tavily_search
        self.sequential_researchers = False
        self._research_capacity = asyncio.Semaphore(2)
        self._tools = None
        self._tools_lock = asyncio.Lock()
        self.tools_by_agent = {
            "supervisor": ["flight_search", "researcher_agent", "itinerary_tool", "tavily_search", "web_search"],
            "researcher": ["trip_segment", "hotel_search", "tavily_search", "web_search", "brave_place_search"],
            "system": ["flight_search", "researcher_agent", "itinerary_tool", "tavily_search", "web_search", "brave_place_search", "trip_segment", "hotel_search"]
        }
        # Initialize the in-memory chat manager
        self.chat_manager = InMemoryChatManager(
            chat_cleanup_interval_minutes=chat_cleanup_interval_minutes,
            chat_expiry_minutes=chat_expiry_minutes
        )
        # Start the cleanup task
        self.chat_manager.start_cleanup_task()

    def recover_failed_turn(self, config):
        """Close pending tool calls so a subsequent user message can resume the chat."""
        key = self._get_chat_history_key(config)
        history = self.chat_manager.get_chat_history(key)
        completed = {message.tool_call_id for message in history if isinstance(message, ToolMessage)}
        for message in history:
            for call in getattr(message, "tool_calls", []):
                if call["id"] not in completed:
                    result = ToolMessage(content="This planning step was interrupted by a provider error. Retry it if still needed.", tool_call_id=call["id"], name=call["name"])
                    self.chat_manager.append_message(key, result)
                    self.db_handler.save_message_to_db(result, config)
                    completed.add(call["id"])

    def load_mcp_config(self) -> Dict[str, Any]:
        with open("trvelle/config/mcp_servers.yaml", 'r') as f:
            mcp_config = yaml.safe_load(f)
        return mcp_config
    
    def _get_chat_history_key(self, config: Dict[str, Any]) -> str:
        """Generate a consistent chat history key from config."""
        chat_id = config.get("chat_id")
        segment_number = config.get("segment_number")
        suffix = ':' + str(config['run_id']) if config.get('run_id') else ''
        
        if segment_number is not None:
            return f"{config.get('user_id')}:{chat_id}{suffix}#{segment_number}"
        return f"{config.get('user_id')}:{chat_id}{suffix}"
    
    async def get_tools(self, requester: str):
        
        async with self._tools_lock:
            if self._tools is None:
                self._tools = await self.mcp_client.get_tools()
                self._tools.append(self.tavily_search)
                self._tools.extend([web_search, brave_place_search])
        tools = self._tools
        tools_to_requester = []
        for tool in tools:
            if tool.name == 'brave_place_search' and search_providers()['places'] != 'brave':
                continue
            if tool.name in self.tools_by_agent[requester]:
                tools_to_requester.append(tool)

        return tools_to_requester, {tool.name: tool for tool in tools_to_requester}
    
    def load_chat_history(self, config: Dict[str, Any]) -> None:
        """Load chat history from database into memory once."""
        user_id = config.get("user_id")
        chat_id = config.get("chat_id")
        segment_number = config.get("segment_number")
        
        history_key = self._get_chat_history_key(config)
        
        # Check if chat history is already loaded
        if self.chat_manager.chat_exists(history_key):
            logger.info(f"Chat history for key '{history_key}' already loaded, skipping database load")
            return
        
        # Load chat history from database
        if user_id and chat_id:
            messages = self.db_handler.load_chat_history(user_id, chat_id, segment_number, config.get('run_id')) if config.get('run_id') else self.db_handler.load_chat_history(user_id, chat_id, segment_number)
            self.chat_manager.set_chat_history(history_key, messages)
            segment_info = f" and segment {segment_number}" if segment_number is not None else ""
            logger.info(f"Loaded {len(messages)} messages from database for user {user_id}, chat {chat_id}{segment_info}")
        else:
            self.chat_manager.set_chat_history(history_key, [])
            logger.warning("No user_id or chat_id provided, starting with empty history")
    
    @db_handler.save_user_input()
    def get_message(self, query: str, config: Optional[Dict[str, Any]] = None) -> HumanMessage:
        """Create a human message and add it to history."""
        message = HumanMessage(content=query, id=str(uuid.uuid4()))
        if config:
            history_key = self._get_chat_history_key(config)
            self.chat_manager.append_message(history_key, message)
        return message
    
    @db_handler.save_output()
    async def call_supervisor_llm(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        await self.checkpoint(config, 'compose' if config.get('finalizing') else 'clarify', 'Planning the next step')
        
        history_key = self._get_chat_history_key(config)
        all_messages = self.chat_manager.get_chat_history(history_key)
        logger.info(f"Calling supervisor with message history length: {len(all_messages)}")
        
        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=f"Today is {date.today().isoformat()}. Never search past travel dates.\n" + SUPERVISOR_INSTRUCTIONS, name="Supervisor_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        formatted_prompt = prompt_template.format_prompt(message=all_messages + [SystemMessage(content=f"Use {config.get('currency', 'USD')} for ALL flight searches, hotel searches, budgets and prices. Keep that currency throughout this trip.")])
        if plan := config.get('continuation_plan'):
            inventory = [{key: h.get(key) for key in ('name', 'choose_uid', 'check_in_date', 'check_out_date', 'total_rate', 'overall_rating', 'selected')}
                         for h in plan.get('travel_options', {}).get('hotels', [])]
            formatted_prompt.messages.append(SystemMessage(content=(
                'The user explicitly continued the saved draft. Work on its unresolved gaps using saved evidence first, '
                'then publish the updated plan with itinerary_tool. Do not merely repeat the previous summary or claim '
                'an update without calling that tool. Keep previously verified selections and notes. Treat this hotel '
                'inventory as authoritative for property names, UIDs and quotes; narrative summaries can be wrong. '
                'Keep the plan partial if any essential stay is unresolved. Remaining gaps and verified hotels: '
                + json.dumps({'unfinished': plan.get('unfinished', []), 'hotels': inventory}, ensure_ascii=False))))
        tools, tools_by_name = await self.get_tools(requester="supervisor")
        if config.get('run_id'):
            from .run_store import store
            count = store.snapshot(config['user_id'], config['run_id'])['counters'].get('supervisor', 0)
            if count >= 6:
                config['finalizing'] = True
                tools = [tool for tool in tools if tool.name == 'itinerary_tool']
                formatted_prompt.messages.append(SystemMessage(content='Finalize now using the saved evidence. Submit itinerary_tool with selected real hotel and flight UIDs. Mark unverified constraints in the description. No further searches or delegation.'))
        response = await self.invoke_model(config, 'supervisor', tools, formatted_prompt)
        config["supervisor_tier"] = response.additional_kwargs["routing_tier"]
        if not response.content and not response.tool_calls:
            raise RuntimeError("The model returned an empty response")
        response.name = "Supervisor_Agent"
        if config.get('continuation_plan') and not response.tool_calls:
            response.content = 'Your saved draft is unchanged. Look up missing information beside the relevant detail in the itinerary, or send a specific change to continue planning.'
        
        self.chat_manager.append_message(history_key, response)
        
        return response
    
    @db_handler.save_output()
    async def call_researcher_llm(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        await self.checkpoint(config, 'research', f"Researching {config.get('destination', 'the destination')}")

        history_key = self._get_chat_history_key(config)
        all_messages = self.chat_manager.get_chat_history(history_key)
        logger.info(f"Calling researcher with message history length: {len(all_messages)}")

        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=RESEARCHER_INSTRUCTIONS, name="Researcher_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        from .research_budget import compact, should_finalize
        finalizing = should_finalize(all_messages)
        instruction = "Research budget: at most 3 hotel searches, 6 web searches and 4 model rounds per segment, including saved work. Reuse verified results. Submit trip_segment as soon as there is enough evidence. Mark unavailable details honestly; never keep searching for perfect results."
        if finalizing:
            instruction += " The research budget is already reached. Submit trip_segment now using the saved evidence. No more searches."
        formatted_prompt = prompt_template.format_prompt(message=compact(all_messages) + [SystemMessage(content=instruction)] + [SystemMessage(content=f"Use {config.get('currency', 'USD')} for ALL flight searches, hotel searches, budgets and prices. Keep that currency throughout this trip.")])
        tools, tools_by_name = await self.get_tools(requester="researcher")
        if finalizing:
            tools = [tool for tool in tools if tool.name == "trip_segment"]
        if config.get('run_id'):
            from .run_store import store
            snap = store.snapshot(config['user_id'], config['run_id'])
            if snap['counters'].get(f"researcher:{config.get('segment_number')}", 0) >= 3:
                tools = [tool for tool in tools if tool.name == 'trip_segment']
                formatted_prompt.messages.append(SystemMessage(content='Submit trip_segment now with saved search evidence and exact hotel UIDs. No more research.'))
            changes = [item['message'] for item in snap['queued_changes'] if item['status'] == 'applied']
            if changes:
                formatted_prompt.messages.append(SystemMessage(content='Apply these user changes to this segment: ' + '\n'.join(changes)))
        response = await self.invoke_model(config, 'researcher', tools, formatted_prompt)
        if not response.content and not response.tool_calls:
            raise RuntimeError("The model returned an empty response")
        response.name = "Researcher_Agent_"+str(config.get("segment_number"))
        self.chat_manager.append_message(history_key, response)

        return response

    async def checkpoint(self, config, phase, task):
        if not config or not config.get('run_id'):
            return
        from .run_store import store
        # Changes are appended only at a tool/model boundary, preserving call/result pairs.
        # Only an orchestrator boundary after all outstanding tool results is safe
        # for a new user message. Researchers can read queued preferences meanwhile.
        main = {k: v for k, v in config.items() if k not in ('segment_number', 'researcher_id')}
        history = self.chat_manager.get_chat_history(self._get_chat_history_key(main))
        completed = {item.tool_call_id for item in history if isinstance(item, ToolMessage)}
        pending = any(call['id'] not in completed for item in history if isinstance(item, AIMessage) for call in item.tool_calls)
        queued = store.take_steering(config['run_id']) if not pending and 'segment_number' not in config else []
        if queued:
            self.load_chat_history(main)
            for change in queued:
                self.get_message(change['message'], main)
            store.event(config['run_id'], 'change_applied', {'ids': [item['id'] for item in queued]})
        store.checkpoint(config['run_id'], phase, task=task)
        store.event(config['run_id'], 'run', store.snapshot(config['user_id'], config['run_id']))

    async def invoke_model(self, config, role, tools, prompt):
        if not config.get('run_id'):
            return await self.model_router.invoke(role, tools, prompt, config.get('supervisor_tier', 0))
        from .run_store import store
        operation = store.reserve_model(config['run_id'], role, config.get('segment_number'))
        store.operation(config['run_id'], operation, 'model')
        response = await self.model_router.invoke(role, tools, prompt, config.get('supervisor_tier', 0))
        if response.id is None:
            response.id = str(uuid.uuid4())
        response.name = 'Supervisor_Agent' if role == 'supervisor' else 'Researcher_Agent_' + str(config.get('segment_number'))
        # Persist model output before the operation checkpoint is advanced.
        self.db_handler.save_message_to_db(response, config)
        store.operation(config['run_id'], operation, 'model', response.model_dump(mode='json'))
        snapshot = store.snapshot(config['user_id'], config['run_id'])
        models = {**snapshot['models'], role: {'provider': response.additional_kwargs.get('routing_provider'), 'model': response.additional_kwargs.get('routing_model'), 'effort': response.additional_kwargs.get('routing_effort', '')}}
        store.checkpoint(config['run_id'], models=models)
        return response
    
    @db_handler.save_tools_output()
    async def handle_tools(self, tool_calls: List[Dict[str, Any]], agent_type: str = "system", config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        logger.info(f"Handling tool calls for {agent_type}: {[tool_call['name'] for tool_call in tool_calls]}")
        _, tools = await self.get_tools(requester=agent_type)
        messages = []
        raw_messages = []
        
        research_used = {}
        if agent_type == "researcher":
            from .research_budget import counts, LIMITS
            history = self.chat_manager.get_chat_history(self._get_chat_history_key(config)) if config else []
            research_used = counts(history)
        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            tool_call_id = tool_call.get("id", "unknown")
            tool_args = tool_call.get("args", {})
            await self.checkpoint(config, 'validate' if tool_name == 'itinerary_tool' else 'search' if tool_name in ('flight_search', 'hotel_search', 'tavily_search', 'web_search', 'brave_place_search') else 'research', f"{tool_name.replace('_', ' ').title()} · {(config or {}).get('destination', '')}".strip(' ·'))
            
            budget_name = 'tavily_search' if tool_name in ('web_search', 'brave_place_search') else tool_name
            if agent_type == "researcher" and budget_name in research_used:
                if research_used[budget_name] >= LIMITS[budget_name]:
                    messages.append(tool_validator.create_error_response(tool_call_id, tool_name, "Research search budget reached. Reuse the saved results and submit trip_segment now. Mark missing details as unverified."))
                    raw_messages.append(None)
                    continue
                research_used[budget_name] += 1

            # Step 1: Check if tool exists for this agent type
            if not tool_validator.validate_tool_exists(tool_name, tools):
                allowed_tools = list(self.tools_by_agent.get(agent_type, []))
                error_msg = f"Tool '{tool_name}' not available for {agent_type} agent. Available tools for {agent_type}: {allowed_tools}"
                logger.error(error_msg)
                error_message = tool_validator.create_error_response(tool_call_id, tool_name, error_msg)
                messages.append(error_message)
                raw_messages.append(None)
                continue
            
            # Step 2: Validate tool input format
            is_valid, validation_error, validated_args = tool_validator.validate_tool_input(tool_name, tool_args)
            
            if not is_valid:
                logger.error(f"Tool validation failed for {tool_name}: {validation_error}")
                error_message = tool_validator.create_error_response(tool_call_id, tool_name, validation_error)
                messages.append(error_message)
                raw_messages.append(None)
                continue
            
            # Step 3: Execute tool with validated arguments
            try:
                from .run_store import store
                if tool_name in ('flight_search', 'hotel_search') and config:
                    validated_args['search_params']['currency'] = config.get('currency', 'USD')
                    if tool_name == 'hotel_search' and config.get('destination'):
                        validated_args['search_params']['_destination'] = config['destination']
                if tool_name == 'itinerary_tool' and config:
                    config['publication_attempted'] = True
                    issue = self.db_handler.itinerary_search_issue(config['user_id'], config['chat_id'], validated_args.get('itinerary', {}))
                    if issue and issue.startswith('Overnight itineraries require real hotel searches') and config.get('run_id'):
                        state = store.snapshot(config['user_id'], config['run_id'])
                        if state['search_usage']['serpapi'] >= state['search_limits']['serpapi']:
                            draft = validated_args['itinerary']
                            draft['planning_status'] = 'partial'
                            draft['unfinished'] = list(dict.fromkeys([*(draft.get('unfinished') or []), 'Hotels', 'Full trip budget']))
                            issue = self.db_handler.itinerary_search_issue(config['user_id'], config['chat_id'], draft)
                    if issue:
                        messages.append(tool_validator.create_error_response(tool_call_id, tool_name, issue))
                        raw_messages.append(None)
                        continue
                tool = tools[tool_name]
                saved = store.operation(config['run_id'], tool_call_id, 'tool') if config and config.get('run_id') else None
                if saved is not None:
                    response = saved
                elif tool_name == 'flight_search':
                    from trvelle.tools.flight_search import flight_search
                    response = await flight_search(**validated_args)
                elif tool_name == 'hotel_search':
                    from trvelle.tools.hotel_search import hotel_search
                    response = await hotel_search(**validated_args)
                elif tool_name == 'tavily_search' and search_providers()['web'] == 'brave':
                    # Saved histories may still contain the legacy tool name.
                    # Honor the run's selected web provider even on those calls.
                    response = await web_search.ainvoke(validated_args)
                elif tool_name == 'itinerary_tool':
                    from trvelle.tools.itinerary_tool import itinerary_tool, Itinerary
                    enriched = await enrich_itinerary(validated_args['itinerary'])
                    response = await itinerary_tool(Itinerary.model_validate(enriched))
                else:
                    response = await tool.ainvoke(validated_args)
                if isinstance(response, list):
                    response = "".join(part.get("text", "") for part in response if isinstance(part, dict))
                try:
                    if isinstance(response, str):
                        response = json.loads(response)
                except Exception as e:
                    logger.error(f"JSON decode error for tool {tool_name} response: {e}")
                    pass
                if config and config.get('run_id'):
                    from trvelle.utils.secrets import redact_secrets
                    store.operation(config['run_id'], tool_call_id, 'tool', redact_secrets(response))
                if isinstance(response, dict):
                    if "result" in response:
                        response, raw = response["result"], response.get("raw", None)
                    else:
                        response, raw = str(response), None
                else:
                    raw = None

                message = ToolMessage(
                    content=response,
                    tool_call_id=tool_call_id,
                    name=tool_name
                )
                
                raw_messages.append(raw)
                messages.append(message)
                if config and config.get('run_id') and tool_name in ('flight_search', 'hotel_search', 'tavily_search', 'web_search', 'brave_place_search'):
                    snapshot = store.snapshot(config['user_id'], config['run_id'])
                    finding = {'tool': tool_name, 'destination': config.get('destination', ''), 'summary': str(response)[:300], 'operation_id': tool_call_id}
                    sources = [{'title': r.get('title', ''), 'url': r.get('url', '')} for r in (raw or {}).get('results', [])] if isinstance(raw, dict) else []
                    store.checkpoint(config['run_id'], findings=[*snapshot['findings'][-11:], finding], sources=list({s['url']: s for s in snapshot['sources'] + sources if s.get('url')}.values())[-20:])
                    store.event(config['run_id'], 'finding', finding)
                logger.info(f"Successfully executed tool {tool_name} for {agent_type}")
                
            except Exception as e:
                # This should be rare since we validated inputs
                import traceback
                logger.error(f"Error executing tool {tool_name} for {agent_type}: {e}\n{traceback.format_exc()}")
                error_msg = f"Tool execution error: {str(e)}"
                logger.error(f"Unexpected error executing {tool_name}: {error_msg}")
                error_message = tool_validator.create_error_response(tool_call_id, tool_name, error_msg)
                messages.append(error_message)
                raw_messages.append(None)

        if config:
            history_key = self._get_chat_history_key(config)
            self.chat_manager.extend_messages(history_key, messages)
        return messages, raw_messages
    
    @db_handler.save_tools_output()
    async def run_research_tasks(self, research_segments: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        logger.info(f"Running research tasks for segments: {[research_segment['args']['city'] for research_segment in research_segments]}")
        researcher_config = config.copy()
        research_results = []
        destinations = {segment['args'].get('city', '') for segment in research_segments}
        multiple_cities = len(destinations) > 1 or any(re.search(r'\s+(?:and|&|\+|to|→|/)\s+', city, re.I) for city in destinations)
        if config.get('run_id') and multiple_cities:
            from .run_store import store
            from trvelle.database.models import PlanningRun
            with store.sessions() as db:
                run = db.query(PlanningRun).filter_by(run_id=config['run_id']).with_for_update().one()
                run.request = {**run.request, 'search_limits': {**run.request.get('search_limits', {}), 'serpapi':12, 'tavily':12}}
                db.commit()
            config['search_limits'] = {**config.get('search_limits', {}), 'serpapi':12, 'tavily':12}
        for i, research_segment in enumerate(research_segments): # TODO: validate reserach_agent_tool schema
            message = f"Plan this trip segment: {research_segment['args']}"
            researcher_config = config.copy()
            researcher_config["segment_number"] = research_segment["args"]["segment_number"]
            researcher_config["researcher_id"] = research_segment.get("id")
            researcher_config['destination'] = research_segment['args'].get('city', '')
            if self.sequential_researchers:
                research_results.append(await self.orchestrate_research(message, researcher_config))
            else:
                async def limited_research(query, options):
                    async with self._research_capacity:
                        return await self.orchestrate_research(query, options)
                research_results.append(limited_research(message, researcher_config))

        if self.sequential_researchers:
            research_results =  research_results
        else:
            research_results = await asyncio.gather(*research_results)
        messages = []
        for result, segment in zip(research_results, research_segments):
            from .research_budget import hotel_offer_catalog
            segment_config = {**config, 'segment_number': segment['args']['segment_number']}
            evidence = self.chat_manager.get_chat_history(self._get_chat_history_key(segment_config))
            catalog = '\n\n'.join(hotel_offer_catalog(item.content) for item in evidence
                                  if isinstance(item, ToolMessage) and item.name == 'hotel_search' and isinstance(item.content, str))
            result_message = ToolMessage(
                content=result.text + '\n\nAuthoritative hotel search offers (use these exact names, UIDs and quotes over conflicting prose):\n' + catalog if catalog else result.content,
                tool_call_id=segment.get("id"),
                name="Researcher_Agent_"+str(segment["args"]["segment_number"])
            )
            messages.append(result_message)
        
        # Add messages to the main chat history (without segment number)
        history_key = self._get_chat_history_key(config)
        self.chat_manager.extend_messages(history_key, messages)
        return messages, [None] * len(messages)  # Assuming no raw results for now
        
    @staticmethod
    def same_research_query(left, right):
        if left == right:
            return True
        prefix = "Plan this trip segment: "
        if not isinstance(left, str) or not isinstance(right, str) or not left.startswith(prefix) or not right.startswith(prefix):
            return False
        try:
            # JSONB can reorder stored tool arguments. Segment identity must not
            # depend on dictionary display order when reconnecting after restart.
            return ast.literal_eval(left[len(prefix):]) == ast.literal_eval(right[len(prefix):])
        except (SyntaxError, ValueError):
            return False

    def pending_response(self, config):
        """Return only calls lacking saved tool results after the last AI turn."""
        history = self.chat_manager.get_chat_history(self._get_chat_history_key(config))
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if isinstance(message, AIMessage):
                completed = {item.tool_call_id for item in history[index + 1:] if isinstance(item, ToolMessage)}
                pending = [call for call in message.tool_calls if call["id"] not in completed]
                return message.model_copy(update={"tool_calls": pending}) if pending else None
            if isinstance(message, HumanMessage):
                break
        return None

    async def orchestrate_research(self, query: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Orchestrate the research process with in-memory history management."""
        if config:
            self.load_chat_history(config)
        history = self.chat_manager.get_chat_history(self._get_chat_history_key(config)) if config else []
        continuing = any(isinstance(item, HumanMessage) and self.same_research_query(item.content, query) for item in history)
        if continuing:
            for item in reversed(history):
                if isinstance(item, ToolMessage) and item.name == "trip_segment":
                    return item
                if isinstance(item, HumanMessage):
                    break
        if not continuing:
            self.get_message(query, config)
        researcher_response = self.pending_response(config) if continuing else None
        if researcher_response is None:
            researcher_response = await self.call_researcher_llm(config)
        rounds = 0
        while researcher_response.tool_calls:
            rounds += 1
            if rounds > 12:
                raise RuntimeError("Research exceeded the tool round limit")
            tool_calls = researcher_response.tool_calls
            tool_results = await self.handle_tools(tool_calls, "researcher", config)
            for tool_result in tool_results:
                if tool_result.name == "trip_segment":
                    return tool_result
            researcher_response = await self.call_researcher_llm(config)

        return researcher_response

    async def orchestrate(self, query: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Main orchestration method with in-memory history management."""

        if config:
            self.load_chat_history(config)
        self.get_message(query, config)
        supervisor_response = await self.call_supervisor_llm(config)
        rounds = 0
        while supervisor_response.tool_calls:
            rounds += 1
            if rounds > 20:
                raise RuntimeError("Planning exceeded the tool round limit")
            function_calls = supervisor_response.tool_calls

            handover = []
            tool_calls = []
            for tool_call in function_calls:
                if tool_call["name"] == "researcher_agent":
                    logger.info(f"Handing over tool call {tool_call['name']} to researcher agent")
                    handover.append(tool_call)
                else:
                    tool_calls.append(tool_call)

            logger.info(f'Tool calls: {[tool["name"] for tool in tool_calls]}, Handover calls: {[tool["name"] for tool in handover]}')
            await self.handle_tools(tool_calls, "supervisor", config)
            await self.run_research_tasks(handover, config = config)
            supervisor_response = await self.call_supervisor_llm(config)

        return supervisor_response
    
    async def orchestrate_stream(self, query: str, config: Optional[Dict[str, Any]] = None, resume: bool = False) -> AsyncGenerator[str, None]:
        """Main orchestration method with streaming support."""
        if config:
            self.load_chat_history(config)
        
        if not resume:
            self.get_message(query, config)
        
        # Stream initial processing message
        yield  {'progress': "🤔 Processing your request...\n"}
        
        supervisor_response = self.pending_response(config) if resume else None
        if supervisor_response is None:
            supervisor_response = await self.call_supervisor_llm(config)
        yield {"message_id": str(self.db_handler.get_message_uuid(supervisor_response.id))}
        # Stream the initial response content
        if hasattr(supervisor_response, 'content') and supervisor_response.content and not supervisor_response.tool_calls:
            content_lines = supervisor_response.text.split('\n')
            for line in content_lines:
                yield {'message': f'{line}\n'}
        
        # Handle tool calls if present
        rounds = 0
        while supervisor_response.tool_calls:
            rounds += 1
            if rounds > 20:
                raise RuntimeError("Planning exceeded the tool round limit")
            yield {'progress': "🔧 Using tools to gather more information...\n"}
            
            function_calls = supervisor_response.tool_calls
            handover = []
            tool_calls = []
            itinerary = []
            
            for tool_call in function_calls:
                if tool_call["name"] == "researcher_agent":
                    handover.append(tool_call)
                elif tool_call["name"] == "itinerary_tool":
                    itinerary.append(tool_call)
                else:
                    tool_calls.append(tool_call)

            if itinerary:
                itinerary_results = await self.handle_tools(itinerary, "supervisor", config)
                published = [result for result in itinerary_results if result.status != 'error' and 'displayed with UID' in result.content]
                if published:
                    from trvelle.utils.travel_text import itinerary_chat_text
                    identifier = str(self.db_handler.get_message_uuid(published[0].tool_call_id))
                    raw = next((call.get('args', {}).get('itinerary', {}) for call in itinerary if call['id'] == published[0].tool_call_id), {})
                    if config:
                        saved_plan = self.db_handler.get_itinerary(config['user_id'], config['chat_id'], uuid.UUID(identifier))
                        if isinstance(saved_plan, dict) and 'daily_plan' in saved_plan:
                            raw = saved_plan  # Preserve any backend-applied draft status.
                    yield {'message_id': identifier}
                    yield {'message': itinerary_chat_text(raw)}
                    yield {'itinerary': identifier}
                    # Publishing is the terminal step; no redundant narrative model call.
                    return

            if tool_calls:
                yield {'progress': "🔍 Searching for information...\n"}
                await self.handle_tools(tool_calls, "supervisor", config)
            
            if handover:
                yield {'progress': "👥 Consulting specialized researchers...\n"}
                await self.run_research_tasks(handover, config=config)
            
            # Get next response and stream it
            supervisor_response = await self.call_supervisor_llm(config)
            yield {"message_id": str(self.db_handler.get_message_uuid(supervisor_response.id))}
            
            if hasattr(supervisor_response, 'content') and supervisor_response.content and not supervisor_response.tool_calls:
                content_lines = supervisor_response.text.split('\n')
                for line in content_lines:
                    yield {'message': f'{line}\n'}

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get statistics about current memory usage."""
        return self.chat_manager.get_memory_stats()
    
    def force_cleanup(self) -> int:
        """Manually trigger cleanup and return number of chats removed."""
        return self.chat_manager.force_cleanup()
    
    async def shutdown(self) -> None:
        """Clean shutdown of the orchestrator."""
        await self.chat_manager.stop_cleanup_task()
        logger.info("Orchestrator shutdown complete")
    
if __name__ == "__main__":

    # example tool call
    # flight_search_input = FlightSearchInput(
    #     flight_legs=[
    #         FlightLeg(departure_id="CDG", arrival_id="NRT", date="2025-06-01"),
    #         FlightLeg(departure_id="NRT", arrival_id="LAX,SEA", date="2025-06-08"),
    #         FlightLeg(departure_id="LAX,SEA", arrival_id="AUS", date="2025-06-15", times="8,18,9,23")
    #     ],
    #     adults=2
    # )
    # orchestrator = Orchestrator()
    # loop = asyncio.get_event_loop()
    # tools, _ = loop.run_until_complete(orchestrator.get_tools())
    # response, _ = loop.run_until_complete(tools[0].ainvoke({"search_params":flight_search_input.model_dump()}))
    # message = ToolMessage(
    #     content=response,
    #     tool_call_id="12345",
    #     name="FlightSearch")
    # print(message.usage_metadata, "_", message.tool_call_id,)
    orchestrator = Orchestrator()
    message = "do a flight search from paris to tokyo on 2025-06-01 and return the cheapest flight for 1 person"
    namespace = uuid.NAMESPACE_DNS  # or any other namespace
    user_id = "user"
    chat_id = "3"
    user_id = uuid.uuid5(namespace, user_id)
    chat_id = uuid.uuid5(namespace, chat_id)
    config = {"user_id": user_id, "chat_id": chat_id}
    
    loop = asyncio.get_event_loop()
    response = loop.run_until_complete(orchestrator.orchestrate(message, config))
    print(response)  # Print the final response in a readable format
