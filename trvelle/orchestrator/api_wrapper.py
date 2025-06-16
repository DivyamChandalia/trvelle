from trvelle.orchestrator import Orchestrator
from trvelle.database import DBHandler
from trvelle.utils import get_logger, load_environment
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import List, Optional, AsyncGenerator
import uuid
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
load_environment()
logger = get_logger(__name__)

app = FastAPI(title="Trvelle", version="0.0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = Orchestrator()
db_handler = DBHandler()

class ChatRequest(BaseModel):
    message: str = Field(..., description="User message")
    user_id: Optional[str] = Field(None, description="User ID (will be generated if not provided)")
    chat_id: Optional[str] = Field(None, description="Chat ID (will be generated if not provided)")
    stream: bool = Field(True, description="Whether to stream the response")

class ChatResponse(BaseModel):
    response: str
    message_id: str
    user_id: str
    chat_id: str

@app.post("/chat")
async def chat(request: ChatRequest):
    print(f"Received request: {request}")
    config = {
        "user_id": uuid.UUID(request.user_id) if request.user_id else uuid.uuid4(),
        "chat_id": uuid.UUID(request.chat_id) if request.chat_id else uuid.uuid4(),
    }

    if request.stream:
        return StreamingResponse(
            stream_chat_response(request.message, config),
            media_type="text/plain; charset=utf-8"
        )
    else:
        response = await orchestrator.orchestrate(
            query=request.message,
            config=config
        )
        
        if hasattr(response, 'content'):
            response_content = response.content
        elif isinstance(response, str):
            response_content = response
        else:
            response_content = "I apologize, but I encountered an issue processing your request."

        return ChatResponse(
            response=response_content,
            user_id=str(config["user_id"]),
            chat_id=str(config["chat_id"]),
            message_id=str(response.message_id) if hasattr(response, 'message_id') else str(uuid.uuid4())
        )

async def stream_chat_response(message: str, config: dict) -> AsyncGenerator[str, None]:
    """Stream the chat response as it's being generated."""
    try:
        message_id = str(uuid.uuid4())
        
        # First: Send the message ID as a JSON chunk
        yield json.dumps({"messageId": message_id}) + "\n"
        
        # Add a small delay to simulate the example
        await asyncio.sleep(0.5)
        
        # Then: Stream the response content
        async for chunk in orchestrator.orchestrate_stream(query=message, config=config):
            yield chunk
            await asyncio.sleep(0.1)  # Small delay between chunks
            
    except Exception as e:
        logger.error(f"Error in stream_chat_response: {e}")
        yield f"Error: {str(e)}\n"


if __name__ == "__main__":
    uvicorn.run(
        app,
        port=8001
    )