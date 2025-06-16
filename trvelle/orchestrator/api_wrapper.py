from trvelle.orchestrator import Orchestrator
from trvelle.database import DBHandler
from trvelle.utils import get_logger, load_environment
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import List, Optional, AsyncGenerator, Dict, Any
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

class ChatHistoryMessage(BaseModel):
    message_id: str
    type: str
    content: str
    message_name: Optional[str] = None
    created_at: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

class ChatHistoryResponse(BaseModel):
    messages: List[ChatHistoryMessage]
    user_id: str
    chat_id: str
    total_messages: int

class ChatSummary(BaseModel):
    chat_id: str
    session_name: Optional[str] = None
    created_at: str
    updated_at: str
    is_active: bool
    message_count: int
    last_message_content: Optional[str] = None
    last_message_type: Optional[str] = None
    last_message_time: Optional[str] = None

class UserChatsResponse(BaseModel):
    chats: List[ChatSummary]
    user_id: str
    total_chats: int

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

@app.get("/chat")
async def get_chat_history(
    user_id: str = Query(..., description="User ID"),
    chat_id: str = Query(..., description="Chat ID")
):
    """
    Get filtered chat history containing all human messages and all supervisor agent messages.
    
    Returns all human messages and all supervisor AI responses in chronological order.
    This filters out tool calls, researcher agents, and other intermediate messages.
    """
    try:
        # Convert string IDs to UUIDs
        user_uuid = uuid.UUID(user_id)
        chat_uuid = uuid.UUID(chat_id)
        
        # Get filtered chat history from database
        messages = db_handler.get_filtered_chat_history(user_uuid, chat_uuid)
        
        # Convert to response format
        chat_history_messages = [
            ChatHistoryMessage(**message) for message in messages
        ]
        
        return ChatHistoryResponse(
            messages=chat_history_messages,
            user_id=user_id,
            chat_id=chat_id,
            total_messages=len(chat_history_messages)
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {str(e)}")
    except Exception as e:
        logger.error(f"Error retrieving chat history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/chats")
async def get_user_chats(
    user_id: str = Header(..., description="User ID from authorization header")
):
    """
    Get all chat sessions for a given user.
    
    Returns a list of all chat sessions for the specified user, ordered by most recently updated.
    Includes metadata such as message count, last message preview, and session details.
    User ID should be provided in the 'user-id' header.
    """
    try:
        # Convert string ID to UUID
        user_uuid = uuid.UUID(user_id)
        
        # Get user chats from database
        chats = db_handler.get_user_chats(user_uuid)
        
        # Convert to response format
        chat_summaries = [
            ChatSummary(**chat) for chat in chats
        ]
        
        return UserChatsResponse(
            chats=chat_summaries,
            user_id=user_id,
            total_chats=len(chat_summaries)
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {str(e)}")
    except Exception as e:
        logger.error(f"Error retrieving user chats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    uvicorn.run(
        app,
        port=8001
    )