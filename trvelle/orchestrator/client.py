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
from ..utils import get_logger, load_environment
from ..prompts import SUPERVISOR_INSTRUCTIONS, RESEARCHER_INSTRUCTIONS
from ..database import DBHandler
load_environment()
logger = get_logger(__name__)

class Orchestrator:
    db_handler = DBHandler()
    def __init__(self):
        self.supervisor = ChatGoogleGenerativeAI(model="gemini-2.5-flash-preview-04-17") # TODO: Check handle_parsing_error
        self.researcher = ChatGoogleGenerativeAI(model="gemini-2.5-flash-preview-04-17")
        self.mcp_config = self.load_mcp_config()
        self.sequential_researchers = True

    def load_mcp_config(self) -> Dict[str, Any]:
        with open("trvelle/config/mcp_servers.yaml", 'r') as f:
            mcp_config = yaml.safe_load(f)
        return mcp_config
    
    async def get_tools(self):
        client = MultiServerMCPClient(self.mcp_config['mcp_servers'])
        tools = await client.get_tools()
        return tools, {tool.name: tool for tool in tools}
    
    @db_handler.attach_user()
    async def call_supervisor_llm(self, message: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=SUPERVISOR_INSTRUCTIONS, name="Supervisor_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        message = prompt_template.format_prompt(message=message)
        tools, tools_by_name = await self.get_tools()
        response = await self.supervisor.bind_tools(tools).ainvoke(message)
        return response
    
    async def call_researcher_llm(self, message: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        prompt_template = ChatPromptTemplate.from_messages([
            SystemMessage(content=RESEARCHER_INSTRUCTIONS, name="Researcher_System_Message"),
            MessagesPlaceholder(variable_name="message")
        ])
        message = prompt_template.format_prompt(message=message)
        tools, tools_by_name = await self.get_tools()
        response = await self.researcher.bind_tools(tools).ainvoke(message)
        return response
    
    async def handle_tools(self, tool_calls: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        _, tools = await self.get_tools()
        results = []
        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            if tool_name not in tools:
                raise ValueError(f"Tool {tool_name} not found in available tools.")
            
            tool = tools[tool_name]
            params = tool_call.get("params", {})
            response = await tool.ainvoke(params)
            results.append({
                "tool_name": tool_name,
                "response": response
            })
        return results
    
    async def handover(self, tool_results: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """ Check if message requires handover to researcher """
        research_results = []
        for tool_result in tool_results:
            if tool_result.get("research_segments"):
                research_result = await self.run_research_tasks(tool_result["research_segments"], config)
                research_results.append(research_result)
        return research_results
    
    async def run_research_tasks(self, research_segments: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
            research_results = []
            for i, research_segment in enumerate(research_segments):
                message = HumanMessage(content=f"Plan this trip segment: {research_segment}")
                segment_number = research_segment.get("segment_number", i)
                config['chat_id'] = f'{config['chat_id']}-{segment_number}'
                if self.sequential_researchers:
                    research_results.append(await self.call_researcher_llm(message, config))
                else:
                    research_results.append(self.call_researcher_llm(message, config))
            
            if self.sequential_researchers:
                return research_results
            else:
                return await asyncio.gather(*research_results)

    async def orchestrate(self, message: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        supervisor_response = await self.call_supervisor_llm(message, config)
        while supervisor_response.tool_calls:
            tool_calls = supervisor_response.tool_calls
            tool_results = await self.handle_tools(tool_calls, config)
            if "final_itinerary" in tool_results: return tool_results["final_itinerary"]
            research_results = await self.handover(tool_results, config)
            supervisor_response = await self.call_supervisor_llm(research_results, config)

        return supervisor_response
    
if __name__ == "__main__":
    orchestrator = Orchestrator()
    message = [HumanMessage(content="Plan a trip from Paris to Rome with a stop in Barcelona.")]
    config = {"user_id": "123", "chat_id": "12345"}
    
    loop = asyncio.get_event_loop()
    response = loop.run_until_complete(orchestrator.orchestrate(message, config))
    print(response)  # Print the final response in a readable format
