from .models import Base, User, ChatSession, Message, ToolCall, ToolResponse, FinalItinerary, ResearcherAgent
from typing import List, Dict, Any, Optional
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from .init_db import create_tables
import functools
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from ..utils import load_environment
from uuid import UUID
import os
from ..utils import get_logger
logger = get_logger(__name__)
load_environment()

class DBHandler:
    engine = create_engine(os.getenv("DB_URI"))
    db_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @classmethod
    def get_messages(cls, user_id:UUID, chat_id: UUID) -> List[BaseMessage]:
        """Retrieve messages for a given chat session ID."""
        db = cls.db_session()

        messages = db.query(Message).filter(Message.chat_id == chat_id).all()
        if not messages:
            new_chat = ChatSession(user_id=user_id, chat_id=chat_id)
            db.add(new_chat)
            db.commit()
        langchain_messages = []
        for message in messages:
            if message.type == "human":
                message = HumanMessage(content=message.content, id=message.message_id)
            elif message.type == "ai":
                message = AIMessage(content=message.content, id=message.message_id, additional_kwargs=message.additional_kwargs)
            elif message.type == "tool":
                message = ToolMessage(
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    id=message.message_id,
                    name=message.tool_name,
                    status=message.status,
                    additional_kwargs=message.additional_kwargs
                )
            else:
                logger.error(f"Unknown message type: {message.type} for message ID: {message.message_id}")
                continue
            langchain_messages.append(message)

        return langchain_messages

    @classmethod
    def attach_user(cls):
        def decorator(func):
            @functools.wraps(func)
            async def wrapper(self, message: List[BaseMessage], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                db = cls.db_session()
                user_id = config.get("user_id") if config else None
                chat_id = config.get("chat_id") if config else None
                existing_user = db.query(User).filter(User.user_id == user_id).first()

                if existing_user:
                    messages = cls.get_messages(chat_id)
                else:
                    new_user = User(user_id=user_id)
                    db.add(new_user)
                    db.commit()
                    messages = []
                messages.extend(message)
                # Attach the user ID before invoking the function
                response = await func(self, messages, config)
                # Save the messages to the database
                msg_to_db = Message(
                    chat_id=chat_id,
                    user_id=user_id,
                    content=response.content,
                    type=response.type,
                    tool_call_id=response.tool_call_id if isinstance(response, ToolMessage) else None,
                    tool_name=response.name if isinstance(response, ToolMessage) else None,
                    status=response.status if isinstance(response, ToolMessage) else None,
                    additional_kwargs=response.additional_kwargs if hasattr(response, 'additional_kwargs') else None
                )
                db.add(msg_to_db)
                db.commit()

                return response
            return wrapper
        return decorator