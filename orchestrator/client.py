from mcp import ClientSession
from typing import Optional, List
import yaml
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
import asyncio


# with open("config/mcp_servers.yaml", 'r') as f:
#     mcp_config = yaml.safe_load(f)

# print(mcp_config)

async def get_tools():
    with open("config/mcp_servers.yaml", 'r') as f:
        mcp_config = yaml.safe_load(f)
    # print(mcp_config)
    client = MultiServerMCPClient(mcp_config['mcp_servers'])
    tools = await client.get_tools()
    tool_names = [tool.name for tool in tools]
    print(tool_names)
    print(tools[0].invoke({}))

asyncio.run(get_tools())
        