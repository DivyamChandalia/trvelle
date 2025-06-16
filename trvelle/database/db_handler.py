from .models import Base, User, ChatSession, Message, ToolExecution, ResearcherAgent
from typing import List, Dict, Any, Optional
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
import datetime
from .init_db import create_tables
import functools
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from ..utils import load_environment
from uuid import UUID
import os
from .init_db import create_tables
from ..utils import get_logger
import uuid
logger = get_logger(__name__)
load_environment()

class DBHandler:
    create_tables()
    engine = create_engine(os.getenv("DB_URI"))
    db_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @staticmethod
    def get_message_uuid(message_id):
        if isinstance(message_id, str):
            # Convert string to UUID - handle LangChain format "run--{uuid}-0"
            cleaned_id = message_id
            if cleaned_id.startswith("run--"):
                cleaned_id = cleaned_id[5:]  # Remove "run--" prefix
            if cleaned_id.endswith("-0"):
                cleaned_id = cleaned_id[:-2]  # Remove "-0" suffix only at the end
            return uuid.UUID(cleaned_id)
        elif isinstance(message_id, uuid.UUID):
            # Convert UUID to string in LangChain format
            return f'run--{str(message_id)}-0'
        elif message_id is None:
            # Handle None case
            return uuid.uuid4()
        else:
            raise TypeError("Expected str or UUID input.")

    @classmethod
    def load_chat_history(cls, user_id: UUID, chat_id: UUID, segment_number: int = None) -> List[BaseMessage]:
        """Load complete chat history once for in-memory management."""
        db = cls.db_session()
        
        # Ensure user exists
        existing_user = db.query(User).filter(User.user_id == user_id).first()
        if not existing_user:
            new_user = User(user_id=user_id)
            db.add(new_user)
            db.commit()
        
        # Ensure chat session exists
        chat_session = db.query(ChatSession).filter(
            ChatSession.user_id == user_id,
            ChatSession.chat_id == chat_id
        ).first()
        if not chat_session:
            new_chat = ChatSession(user_id=user_id, chat_id=chat_id)
            db.add(new_chat)
            db.commit()

        if segment_number:
            # Load messages for the researcher agent
            messages = db.query(ResearcherAgent).filter(
                ResearcherAgent.chat_id == chat_id,
                ResearcherAgent.segment_number == segment_number
            ).order_by(ResearcherAgent.created_at.asc()).all()
        else:
            # Load all messages for this chat
            messages = db.query(Message).filter(
                Message.user_id == user_id,
                Message.chat_id == chat_id
            ).order_by(Message.created_at.asc()).all()
            
        langchain_messages = []
        for message in messages:
            if message.type == "human":
                message = HumanMessage(content=message.content, id=str(message.message_id))
            elif message.type == "ai":
                message = AIMessage(content=message.content, 
                                    id=cls.get_message_uuid(message.message_id), 
                                    additional_kwargs=message.additional_kwargs,
                                    name=message.message_name)
            elif message.type == "tool":
                message = ToolMessage(
                    content=message.content,
                    tool_call_id=str(message.message_id),
                    name=message.message_name
                )
            else:
                logger.error(f"Unknown message type: {message.type} for message ID: {message.message_id}")
                continue
            langchain_messages.append(message)

        db.close()
        return langchain_messages

    @classmethod
    def save_message_to_db(cls, message: BaseMessage, config: Dict[str, Any]) -> None:
        """Save a single message to database."""
        db = cls.db_session()
        msg_to_db = cls.get_message_from_response(message, config)
        db.add(msg_to_db)
        db.commit()
    
    @classmethod
    def get_message_from_response(cls, response: Dict[str, Any], config: Dict) -> Message:
        usage_metadata = response.usage_metadata if hasattr(response, 'usage_metadata') and response.usage_metadata is not None else {
            'input_tokens': None,
            'output_tokens': None,
            'total_tokens': None,
            'input_token_details': {'cache_read': None},
            'output_token_details': {'reasoning': None}
        }
        
        # Common attributes for both models
        common_attrs = {
            'chat_id': config.get("chat_id"),
            'message_id': cls.get_message_uuid(response.tool_call_id) if response.type == "tool" else cls.get_message_uuid(response.id),
            'content': response.content,
            'message_name': response.name if hasattr(response, 'name') else None,
            'type': response.type,
            'additional_kwargs': response.tool_calls if hasattr(response, 'tool_calls') else response.additional_kwargs,
            'input_tokens': usage_metadata['input_tokens'],
            'output_tokens': usage_metadata['output_tokens'],
            'total_tokens': usage_metadata['total_tokens'],
            'cache_tokens': usage_metadata['input_token_details']['cache_read'] if 'input_token_details' in usage_metadata else None,
            'reasoning_tokens': usage_metadata['output_token_details']['reasoning'] if 'output_token_details' in usage_metadata else None,
            'created_at': datetime.datetime.now(datetime.timezone.utc)
        }
        
        if "researcher_id" in config:
            return ResearcherAgent(
                **common_attrs,
                parent_message_id=cls.get_message_uuid(config["researcher_id"]),
                segment_number=config["segment_number"],
            )
        else:
            return Message(
                **common_attrs,
                user_id=config.get("user_id"),
            )
    
    @classmethod
    def save_user_input(cls):
        """Decorator to save user input messages to database."""
        def decorator(func):
            @functools.wraps(func)
            def wrapper(self, query: str, config: Optional[Dict[str, Any]] = None) -> HumanMessage:
                # Execute the function
                message = func(self, query, config)
                
                # Save the user message to database
                if config:
                    cls.save_message_to_db(message, config)
                
                return message
            return wrapper
        return decorator
    
    @classmethod
    def save_output(cls):
        """Decorator to save supervisor agent outputs to database."""
        def decorator(func):
            @functools.wraps(func)
            async def wrapper(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                # Execute the function
                response = await func(self, config)
                
                # Save the supervisor response to database
                if config:
                    cls.save_message_to_db(response, config)
                
                return response
            return wrapper
        return decorator
    
    @classmethod
    def save_tools_output(cls):
        """Decorator to save tool execution outputs to database."""
        def decorator(func):
            @functools.wraps(func)
            async def wrapper(self, tool_calls: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None):
                # Execute the function
                messages, raw_messages = await func(self, tool_calls, config)

                # Save the tool messages to the database
                if config:
                    db = cls.db_session()
                    for (message, raw) in zip(messages, raw_messages):
                        msg_to_db = cls.get_message_from_response(message, config)
                        db.add(msg_to_db)
                        if raw:
                            tool_execution = ToolExecution(
                                chat_id=config.get("chat_id"),
                                message_id=msg_to_db.message_id,
                                tool_name=message.name,
                                raw_response=raw,
                                created_at=datetime.datetime.now(datetime.timezone.utc)
                            )
                            db.add(tool_execution)
                    db.commit()
                
                return messages
            return wrapper
        return decorator
    
    @classmethod
    def get_filtered_chat_history(cls, user_id: UUID, chat_id: UUID) -> List[Dict[str, Any]]:
        """Get chat history with all human messages and all supervisor agent messages."""
        db = cls.db_session()
        
        try:
            # Get all messages for this chat ordered by creation time
            messages = db.query(Message).filter(
                Message.user_id == user_id,
                Message.chat_id == chat_id
            ).order_by(Message.created_at.asc()).all()
            
            filtered_messages = []
            
            for message in messages:
                # Include human messages and supervisor agent messages
                if (message.type == "human" or 
                    (message.type == "ai" and message.message_name == "Supervisor_Agent" and message.content is not None)):
                    
                    filtered_messages.append({
                        "message_id": str(message.message_id),
                        "type": message.type,
                        "content": message.content,
                        "message_name": message.message_name,
                        "created_at": message.created_at.isoformat(),
                        "input_tokens": message.input_tokens,
                        "output_tokens": message.output_tokens,
                        "total_tokens": message.total_tokens
                    })
            
            return filtered_messages
            
        finally:
            db.close()
    
    @classmethod
    def get_user_chats(cls, user_id: UUID) -> List[Dict[str, Any]]:
        """Get all chat sessions for a given user with basic metadata."""
        db = cls.db_session()
        
        try:
            # Get all chat sessions for this user
            chat_sessions = db.query(ChatSession).filter(
                ChatSession.user_id == user_id
            ).order_by(ChatSession.updated_at.desc()).all()
            
            user_chats = []
            for chat in chat_sessions:
                # Get the count of messages in this chat
                message_count = db.query(Message).filter(
                    Message.chat_id == chat.chat_id,
                    Message.user_id == user_id
                ).count()
                
                # Get the last message for preview
                last_message = db.query(Message).filter(
                    Message.chat_id == chat.chat_id,
                    Message.user_id == user_id
                ).order_by(Message.created_at.desc()).first()
                
                user_chats.append({
                    "chat_id": str(chat.chat_id),
                    "session_name": chat.session_name,
                    "created_at": chat.created_at.isoformat(),
                    "updated_at": chat.updated_at.isoformat(),
                    "is_active": chat.is_active,
                    "message_count": message_count,
                    "last_message_content": last_message.content if last_message else None,
                    "last_message_type": last_message.type if last_message else None,
                    "last_message_time": last_message.created_at.isoformat() if last_message else None
                })
            
            return user_chats
            
        finally:
            db.close()