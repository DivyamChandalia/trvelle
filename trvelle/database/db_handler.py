from .models import Base, User, ChatSession, Message, ToolExecution, FinalItinerary, ResearcherAgent
from typing import List, Dict, Any, Optional
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
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
            print(f"Original: '{message_id}' -> Cleaned: '{cleaned_id}'")
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
    def load_chat_history(cls, user_id: UUID, chat_id: UUID, researcher_id: UUID = None, segment_number: int = None) -> List[BaseMessage]:
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

        if researcher_id:
            # Load messages for the researcher agent
            messages = db.query(ResearcherAgent).filter(
                ResearcherAgent.chat_id == chat_id,
                ResearcherAgent.parent_message_id == researcher_id,
                ResearcherAgent.segment_number == segment_number
            ).order_by(ResearcherAgent.created_at.asc()).all()
            
            langchain_messages = []
            for message in messages:
                if message.type == "human":
                    message = HumanMessage(content=message.content, id=str(message.agent_execution_id))
                elif message.type == "ai":
                    message = AIMessage(content=message.content, 
                                        id=cls.get_message_uuid(message.agent_execution_id), 
                                        additional_kwargs=message.additional_kwargs,
                                        name=message.message_name)
                elif message.type == "tool":
                    message = ToolMessage(
                        content=message.content,
                        tool_call_id=str(message.agent_execution_id),
                        name=message.message_name
                    )
                else:
                    logger.error(f"Unknown message type: {message.type} for message ID: {message.agent_execution_id}")
                    continue
                langchain_messages.append(message)
        
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
    def get_messages_from_db(cls, user_id:UUID, chat_id: UUID) -> List[BaseMessage]:
        """Retrieve messages for a given chat session ID."""
        db = cls.db_session()

        chat_session = db.query(ChatSession).filter(
            ChatSession.user_id == user_id,
            ChatSession.chat_id == chat_id
        ).first()
        if not chat_session:
            new_chat = ChatSession(user_id=user_id, chat_id=chat_id)
            db.add(new_chat)
            db.commit()
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

        return langchain_messages
    
    @classmethod
    def get_message_from_response(cls, response: Dict[str, Any], config: Dict) -> Message:
        user_id = config.get("user_id")
        chat_id = config.get("chat_id")
        usage_metadata = response.usage_metadata if hasattr(response, 'usage_metadata') else {
            'input_tokens': None,
            'output_tokens': None,
            'total_tokens': None,
            'input_token_details': {'cache_read': None},
            'output_token_details': {'reasoning': None}
        }
        if "researcher_id" in config.keys():
            msg_to_db = ResearcherAgent(
                chat_id=chat_id,
                agent_execution_id=uuid.UUID(response.tool_call_id) if response.type == "tool" else cls.get_message_uuid(response.id),
                parent_message_id=uuid.UUID(config["researcher_id"]),
                segment_number=config["segment_number"],
                content=response.content,
                message_name=response.name if hasattr(response, 'name') else None,
                type=response.type,
                additional_kwargs=response.additional_kwargs,
                input_tokens=usage_metadata['input_tokens'],
                output_tokens=usage_metadata['output_tokens'],
                total_tokens=usage_metadata['total_tokens'],
                cache_tokens=usage_metadata['input_token_details']['cache_read'],
                reasoning_tokens=usage_metadata['output_token_details']['reasoning'],
            )
        else:
            msg_to_db = Message(
                chat_id=chat_id,
                user_id=user_id,
                content=response.content,
                message_name=response.name if hasattr(response, 'name') else None,
                type=response.type,
                message_id=uuid.UUID(response.tool_call_id) if response.type == "tool" else cls.get_message_uuid(response.id),
                additional_kwargs=response.additional_kwargs,
                input_tokens=usage_metadata['input_tokens'],
                output_tokens=usage_metadata['output_tokens'],
                total_tokens=usage_metadata['total_tokens'],
                cache_tokens=usage_metadata['input_token_details']['cache_read'],
                reasoning_tokens=usage_metadata['output_token_details']['reasoning'],
            )
        return msg_to_db
    
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
                                raw_response=raw
                            )
                            db.add(tool_execution)
                    db.commit()
                
                return messages
            return wrapper
        return decorator