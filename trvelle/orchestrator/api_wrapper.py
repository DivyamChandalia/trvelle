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
    created_at: str

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

@app.get("/chat")
async def chat(
    request: ChatRequest,
    user_id: str = Header(None, description="User ID from authorization header"),
    chat_id: Optional[str] = Query(None, description="Chat ID from authorization header")
):
    print(f"Received request: {request} with user_id: {user_id} and chat_id: {chat_id}")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")
    config = {
        "user_id": uuid.UUID(user_id),
        "chat_id": uuid.UUID(chat_id) if chat_id else uuid.uuid4(),
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
        
        # Send metadata as SSE event
        yield f"event: user_id\ndata: {config['user_id']}\n\n"
        yield f"event: chat_id\ndata: {config["chat_id"]}\n\n"
        yield f"event: message_id\ndata: {message_id}\n\n"
        
        # Stream the response content as message events
        async for chunk in orchestrator.orchestrate_stream(query=message, config=config):
            
            if isinstance(chunk, dict) and "message" in chunk:
                yield f"event: message\ndata: {chunk['message']}\n\n"
            elif isinstance(chunk, dict) and "progress" in chunk:
                yield f"event: progress\ndata: {chunk["progress"]}\n\n"
            elif isinstance(chunk, dict) and "itinerary" in chunk:
                yield f"event: itinerary_id\ndata: {chunk["itinerary"]}\n\n"
        
        # Send itinerary ID as separate event
        yield f"event: itinerary_id\ndata: {'3b53f560-a08d-461e-86ba-8357b1ac1fbb'}\n\n"
            
    except Exception as e:
        logger.error(f"Error in stream_chat_response: {e}")
        import traceback
        logger.error(traceback.format_exc())
        yield f"event: error\ndata: {str(e)}\n\n"

@app.get("/chat_history")
async def get_chat_history(
    user_id: str = Header(..., description="User ID"),
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

@app.get("/list_chats")
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
    
@app.get("/tool_call")
async def get_tool_response(
    user_id: str = Header(..., description="User ID authorization header"),
    chat_id: str = Query(..., description="Chat ID from authorization header"),
    tool_call_id: str = Header(..., description="tool_response ID from authorization header")
):
    """
    Get the tool_response for a specific chat session.
    
    Returns the structured tool_response data for the specified chat session.
    User ID and chat ID should be provided in the 'user-id' and 'chat-id' headers.
    """
    try:
        user_id = uuid.UUID(user_id)
        chat_id = uuid.UUID(chat_id)

        try:
            tool_call_id = uuid.UUID(tool_call_id)

            itinerary = db_handler.get_itinerary(user_id, chat_id, tool_call_id)
            if not itinerary:
                raise HTTPException(status_code=404, detail="Itinerary not found")
            
            return itinerary
        
        except ValueError:
            tool_response = db_handler.get_tool_response(user_id, chat_id, tool_call_id)
            if not tool_response:
                raise HTTPException(status_code=404, detail="Tool response not found")
            return tool_response

    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {str(e)}")
    except Exception as e:
        logger.error(f"Error retrieving itinerary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.delete("/chat")
async def delete_chat(
    user_id: str = Header(..., description="User ID"),
    chat_id: str = Query(..., description="Chat ID to delete")
):
    """
    Delete a chat session and all associated data for a user.
    
    Deletes the specified chat session along with all related data including:
    - All messages in the chat
    - All tool executions in the chat
    - All researcher agent records in the chat
    
    User ID should be provided in the 'user-id' header.
    Chat ID should be provided as a query parameter.
    """
    try:
        # Convert string IDs to UUIDs
        user_uuid = uuid.UUID(user_id)
        chat_uuid = uuid.UUID(chat_id)
        
        # Delete the chat using the database handler
        result = db_handler.delete_chat(user_uuid, chat_uuid)
        
        # Check if deletion was successful
        if "error" in result:
            if result["error"] == "User not found":
                raise HTTPException(status_code=404, detail="User not found")
            elif result["error"] == "Chat session not found":
                raise HTTPException(status_code=404, detail="Chat session not found")
            else:
                raise HTTPException(status_code=500, detail=result["error"])
        
        return {
            "success": True,
            "message": result["message"],
            "deleted_chat_id": chat_id,
            "user_id": user_id
        }
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {str(e)}")
    except Exception as e:
        logger.error(f"Error deleting chat: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001
    )