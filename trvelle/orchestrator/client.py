from mcp import ClientSession
from typing import Optional, List
import yaml
from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List
import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage, HumanMessage, BaseMessage
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from ..utils import get_logger, load_environment, configure_logging
from ..prompts import SUPERVISOR_INSTRUCTIONS, RESEARCHER_INSTRUCTIONS
from ..database import DBHandler
from ..tools import FlightSearchInput, FlightLeg
import uuid
load_environment()
configure_logging()
logger = get_logger(__name__)

class Orchestrator:
    db_handler = DBHandler()
    
    def __init__(self):
        self.supervisor = ChatGoogleGenerativeAI(model="gemini-2.5-flash-preview-04-17")
        self.researcher = ChatGoogleGenerativeAI(model="gemini-2.5-flash-preview-04-17")
        self.mcp_config = self.load_mcp_config()
        self.sequential_researchers = True
        self.chat_history = []  # In-memory chat history

    def load_mcp_config(self) -> Dict[str, Any]:
        with open("trvelle/config/mcp_servers.yaml", 'r') as f:
            mcp_config = yaml.safe_load(f)
        return mcp_config
    
    async def get_tools(self):
        client = MultiServerMCPClient(self.mcp_config['mcp_servers'])
        tools = await client.get_tools()
        return tools, {tool.name: tool for tool in tools}

    def load_chat_history(self, config: Dict[str, Any]) -> None:
        """Load chat history from database into memory once."""
        user_id = config.get("user_id")
        chat_id = config.get("chat_id")
        
        if user_id and chat_id:
            self.chat_history = self.db_handler.load_chat_history(user_id, chat_id)
            logger.info(f"Loaded {len(self.chat_history)} messages from database")
        else:
            self.chat_history = []
            logger.warning("No user_id or chat_id provided, starting with empty history")

    @db_handler.save_user_input()
    def get_message(self, query: str, config: Optional[Dict[str, Any]] = None) -> HumanMessage:
        """Create a human message and add it to history."""
        message = HumanMessage(content=query)
        self.chat_history.append(message)
        return message
    
    @db_handler.save_supervisor_output()
    async def call_supervisor_llm(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        logger.info(f"Calling supervisor with message history length: {len(self.chat_history)}")
        
        # Prepare messages for supervisor (full history + current input)
        all_messages = self.chat_history.copy()
        
        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=SUPERVISOR_INSTRUCTIONS, name="Supervisor_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        formatted_prompt = prompt_template.format_prompt(message=all_messages)
        tools, tools_by_name = await self.get_tools()
        response = await self.supervisor.bind_tools(tools).ainvoke(formatted_prompt)
        response.name = "Supervisor_Agent"
        
        # Add supervisor response to history
        self.chat_history.append(response)
        
        return response
    
    async def call_researcher_llm(self, message: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=RESEARCHER_INSTRUCTIONS, name="Researcher_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        formatted_prompt = prompt_template.format_prompt(message=message)
        tools, tools_by_name = await self.get_tools()
        response = await self.researcher.bind_tools(tools).ainvoke(formatted_prompt)
        response.name = "Researcher_Agent"
        return response
    
    @db_handler.save_tools_output()
    async def handle_tools(self, tool_calls: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        logger.info(f"Handling tool calls: {tool_calls}")
        _, tools = await self.get_tools()
        messages = []
        raw_messages = []
        
        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            if tool_name not in tools:
                raise ValueError(f"Tool {tool_name} not found in available tools.")
            
            tool = tools[tool_name]
            response = await tool.ainvoke(tool_call['args'])
            
            if isinstance(response, List):
                response, raw = response
            else:
                raw = None

            message = ToolMessage(
                content=response,
                tool_call_id=tool_call.get("id"),
                name=tool_name)
            
            # Add tool message to history
            self.chat_history.append(message)
            
            raw_messages.append(raw)
            messages.append(message)
        self.chat_history.extend(messages)
        return messages, raw_messages
    
    async def handover(self, tool_results: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check if message requires handover to researcher."""
        logger.info(f"Handover tool results: {tool_results}")
        research_results = []
        for tool_result in tool_results:
            if hasattr(tool_result, "research_segments") and tool_result.get("research_segments"):
                research_result = await self.run_research_tasks(tool_result["research_segments"], config)
                research_results.append(research_result)
        return research_results
    
    async def run_research_tasks(self, research_segments: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        logger.info(f"Running research tasks for segments: {research_segments}")
        research_results = []
        for i, research_segment in enumerate(research_segments):
            message = HumanMessage(content=f"Plan this trip segment: {research_segment}")
            segment_number = research_segment.get("segment_number", i)
            segment_config = config.copy() if config else {}
            segment_config['chat_id'] = f'{config['chat_id']}-{segment_number}'
            
            if self.sequential_researchers:
                research_results.append(await self.call_researcher_llm([message], segment_config))
            else:
                research_results.append(self.call_researcher_llm([message], segment_config))
        
        if self.sequential_researchers:
            return research_results
        else:
            return await asyncio.gather(*research_results)

    async def orchestrate(self, query: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Main orchestration method with in-memory history management."""
        # Load chat history from database once at the beginning
        if config:
            self.load_chat_history(config)
        
        # Create and add human message to history
        message = self.get_message(query, config)
        
        # Call supervisor with full history context
        supervisor_response = await self.call_supervisor_llm( config)
        
        # Handle tool calls and continue conversation
        while supervisor_response.tool_calls:
            tool_calls = supervisor_response.tool_calls
            tool_results = await self.handle_tools(tool_calls, config)
            if "final_itinerary" in str(tool_results): 
                return tool_results["final_itinerary"]
            
            research_results = await self.handover(tool_results, config)
            supervisor_response = await self.call_supervisor_llm(config)

        return supervisor_response
    
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
