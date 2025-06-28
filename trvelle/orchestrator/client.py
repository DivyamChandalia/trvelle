from typing import Optional, List, AsyncGenerator, Any, Dict
import yaml
from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio
import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, ToolMessage, HumanMessage
from langchain_tavily import TavilySearch
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
        self.supervisor = ChatGoogleGenerativeAI(model="gemini-2.5-flash-preview-05-20")
        self.researcher = ChatGoogleGenerativeAI(model="gemini-2.5-flash-preview-05-20")
        self.mcp_config = self.load_mcp_config()
        self.mcp_client = MultiServerMCPClient(self.mcp_config['mcp_servers'])
        self.tavily_search = TavilySearch(api_key=os.getenv("TAVILY_API_KEY"))
        self.sequential_researchers = True
        self.tools_by_agent = {
            "supervisor": ["flight_search", "researcher_agent", "itinerary_tool", "tavily_search"],
            "researcher": ["trip_segment", "hotel_search", "tavily_search"],
            "system": ["flight_search", "researcher_agent", "itinerary_tool", "tavily_search", "trip_segment", "hotel_search"]
        }
        # Initialize the in-memory chat manager
        self.chat_manager = InMemoryChatManager(
            chat_cleanup_interval_minutes=chat_cleanup_interval_minutes,
            chat_expiry_minutes=chat_expiry_minutes
        )
        # Start the cleanup task
        self.chat_manager.start_cleanup_task()

    def load_mcp_config(self) -> Dict[str, Any]:
        with open("trvelle/config/mcp_servers.yaml", 'r') as f:
            mcp_config = yaml.safe_load(f)
        return mcp_config
    
    def _get_chat_history_key(self, config: Dict[str, Any]) -> str:
        """Generate a consistent chat history key from config."""
        chat_id = config.get("chat_id")
        segment_number = config.get("segment_number")
        
        if segment_number is not None:
            return f"{chat_id}#{segment_number}"
        return str(chat_id)
    
    async def get_tools(self, requester: str):
        
        tools = await self.mcp_client.get_tools()
        tools.append(self.tavily_search)
        tools_to_requester = []
        for tool in tools:
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
            messages = self.db_handler.load_chat_history(user_id, chat_id, segment_number)
            self.chat_manager.set_chat_history(history_key, messages)
            segment_info = f" and segment {segment_number}" if segment_number is not None else ""
            logger.info(f"Loaded {len(messages)} messages from database for user {user_id}, chat {chat_id}{segment_info}")
        else:
            self.chat_manager.set_chat_history(history_key, [])
            logger.warning("No user_id or chat_id provided, starting with empty history")
    
    @db_handler.save_user_input()
    def get_message(self, query: str, config: Optional[Dict[str, Any]] = None) -> HumanMessage:
        """Create a human message and add it to history."""
        message = HumanMessage(content=query)
        if config:
            history_key = self._get_chat_history_key(config)
            self.chat_manager.append_message(history_key, message)
        return message
    
    @db_handler.save_output()
    async def call_supervisor_llm(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        
        history_key = self._get_chat_history_key(config)
        all_messages = self.chat_manager.get_chat_history(history_key)
        logger.info(f"Calling supervisor with message history length: {len(all_messages)}")
        
        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=SUPERVISOR_INSTRUCTIONS, name="Supervisor_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        formatted_prompt = prompt_template.format_prompt(message=all_messages)
        tools, tools_by_name = await self.get_tools(requester="supervisor")
        retry = True
        while retry:
            try:
                response = await self.researcher.bind_tools(tools).ainvoke(formatted_prompt)
                if len(response.content)>0 or response.tool_calls:
                    retry = False
                else:
                    logger.info(f"Supervisor response: {response}")
                    await asyncio.sleep(60)  # Retry after a short delay
            except Exception as e:
                logger.error(f"Error calling supervisor: {e}")
                await asyncio.sleep(60)  # Retry after a short delay
        response.name = "Supervisor_Agent"
        
        self.chat_manager.append_message(history_key, response)
        
        return response
    
    @db_handler.save_output()
    async def call_researcher_llm(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:

        history_key = self._get_chat_history_key(config)
        all_messages = self.chat_manager.get_chat_history(history_key)
        logger.info(f"Calling researcher with message history length: {len(all_messages)}")

        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=RESEARCHER_INSTRUCTIONS, name="Researcher_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        formatted_prompt = prompt_template.format_prompt(message=all_messages)
        tools, tools_by_name = await self.get_tools(requester="researcher")
        retry = True
        while retry:
            try:
                response = await self.researcher.bind_tools(tools).ainvoke(formatted_prompt)
                if len(response.content)>0 or response.tool_calls:
                    retry = False
                else:
                    logger.info(f"Supervisor response: {response}")
                    await asyncio.sleep(60)  # Retry after a short delay
            except Exception as e:
                logger.error(f"Error calling supervisor: {e}")
                await asyncio.sleep(60)  # Retry after a short delay
        response.name = "Researcher_Agent_"+str(config.get("segment_number"))
        self.chat_manager.append_message(history_key, response)

        return response
    
    @db_handler.save_tools_output()
    async def handle_tools(self, tool_calls: List[Dict[str, Any]], agent_type: str = "system", config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        logger.info(f"Handling tool calls for {agent_type}: {[tool_call['name'] for tool_call in tool_calls]}")
        _, tools = await self.get_tools(requester=agent_type)
        messages = []
        raw_messages = []
        
        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            tool_call_id = tool_call.get("id", "unknown")
            tool_args = tool_call.get("args", {})
            
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
                tool = tools[tool_name]
                print(f"Executing tool {tool_name} for {agent_type} with args: {validated_args}")
                response = await tool.ainvoke(validated_args)
                try:
                    response = json.loads(response)
                except Exception as e:
                    logger.error(f"JSON decode error for tool {tool_name} response: {e}")
                    pass
                print(f'type(response)={type(response)}, len(response)={len(response) if isinstance(response, (list, str)) else "N/A"}')
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
        for i, research_segment in enumerate(research_segments): # TODO: validate reserach_agent_tool schema
            message = f"Plan this trip segment: {research_segment['args']}"
            researcher_config["segment_number"] = research_segment["args"]["segment_number"]
            researcher_config["researcher_id"] = research_segment.get("id")
            if self.sequential_researchers:
                research_results.append(await self.orchestrate_research(message, researcher_config))
            else:
                research_results.append(self.orchestrate_research(message, researcher_config))

        if self.sequential_researchers:
            research_results =  research_results
        else:
            research_results = await asyncio.gather(*research_results)
        messages = []
        for result, segment in zip(research_results, research_segments):
            result_message = ToolMessage(
                content=result.content,
                tool_call_id=segment.get("id"),
                name="Researcher_Agent_"+str(segment["args"]["segment_number"])
            )
            messages.append(result_message)
        
        # Add messages to the main chat history (without segment number)
        history_key = self._get_chat_history_key(config)
        self.chat_manager.extend_messages(history_key, messages)
        return messages, [None] * len(messages)  # Assuming no raw results for now
        
    async def orchestrate_research(self, query: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Orchestrate the research process with in-memory history management."""
        if config:
            self.load_chat_history(config)
        self.get_message(query, config)
        researcher_response = await self.call_researcher_llm(config)
        while researcher_response.tool_calls:
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
        while supervisor_response.tool_calls:
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
    
    async def orchestrate_stream(self, query: str, config: Optional[Dict[str, Any]] = None) -> AsyncGenerator[str, None]:
        """Main orchestration method with streaming support."""
        if config:
            self.load_chat_history(config)
        
        self.get_message(query, config)
        
        # Stream initial processing message
        yield  {'progress': "🤔 Processing your request...\\n"}
        
        supervisor_response = await self.call_supervisor_llm(config)
        # Stream the initial response content
        if hasattr(supervisor_response, 'content') and supervisor_response.content:
            content_lines = supervisor_response.content.split('\n')
            for line in content_lines:
                if line.strip():
                    yield {'message': f'{line}\\n'}
                    await asyncio.sleep(0.1)  # Simulate streaming delay
        
        # Handle tool calls if present
        while supervisor_response.tool_calls:
            yield {'progress': "🔧 Using tools to gather more information...\\n"}
            
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
                yield {'itinerary': f'{itinerary_results[0].tool_call_id}'}

            if tool_calls:
                yield {'progress': "🔍 Searching for information...\\n"}
                await self.handle_tools(tool_calls, config)
                yield {'progress': "🔍 Searching for information...\n"}
                await self.handle_tools(tool_calls, "supervisor", config)
            
            if handover:
                yield {'progress': "👥 Consulting specialized researchers...\\n"}
                await self.run_research_tasks(handover, config)
            
            # Get next response and stream it
            supervisor_response = await self.call_supervisor_llm(config)
            
            if hasattr(supervisor_response, 'content') and supervisor_response.content: # "Heloo \\n\\n whats up \\n .\\n\\n"
                content_lines = supervisor_response.content.split('\n')
                for line in content_lines:
                    if line.strip():
                        yield {'message': f'{line}\\n'}
                        await asyncio.sleep(0.1)  # Simulate streaming delay

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
