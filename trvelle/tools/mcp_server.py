import contextlib
from fastapi import FastAPI
from .flight_search import mcp as flight_search_mcp
from .hotel_search import mcp as hotel_search_mcp
from .itinerary_tool import mcp as itinerary_tool_mcp
from .researcher_agent_tool import mcp as researcher_agent_tool_mcp
from .trip_segment_tool import mcp as trip_segment_tool_mcp
import uvicorn

# Create a combined lifespan to manage both session managers
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(flight_search_mcp.session_manager.run())
        await stack.enter_async_context(hotel_search_mcp.session_manager.run())
        await stack.enter_async_context(itinerary_tool_mcp.session_manager.run())
        await stack.enter_async_context(researcher_agent_tool_mcp.session_manager.run())
        await stack.enter_async_context(trip_segment_tool_mcp.session_manager.run())
        yield

app = FastAPI(lifespan=lifespan)
@app.post("/")
async def read_root():
    return {"message": "Welcome to the Python MCP server!"}

app.mount("/flight_search/", flight_search_mcp.streamable_http_app())
app.mount("/hotel_search/", hotel_search_mcp.streamable_http_app())
app.mount("/itinerary_tool/", itinerary_tool_mcp.streamable_http_app())
app.mount("/researcher_agent_tool/", researcher_agent_tool_mcp.streamable_http_app())
app.mount("/trip_segment_tool/", trip_segment_tool_mcp.streamable_http_app())
uvicorn.run(app, port=8000)