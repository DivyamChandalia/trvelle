import contextlib
from fastapi import FastAPI
from .flight_search import mcp as flight_search_mcp
import uvicorn

# Create a combined lifespan to manage both session managers
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(flight_search_mcp.session_manager.run())
        yield

app = FastAPI(lifespan=lifespan)
@app.post("/")
async def read_root():
    return {"message": "Welcome to the Python MCP server!"}

app.mount("/flight_search/", flight_search_mcp.streamable_http_app())
uvicorn.run(app, port=8000)