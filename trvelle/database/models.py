"""
PostgreSQL Database Models for PlanIt Travel Assistant

This module contains all the database models for storing user interactions,
chat sessions, messages, tool calls, tool responses, final itineraries, 
and researcher agent history.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, JSON, ForeignKey, 
    Boolean, Float, create_engine, Index
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid

Base = declarative_base()

class User(Base):
    """User table to store user information"""
    __tablename__ = 'users'
    
    user_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(255), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc), nullable=False)
    preferences = Column(JSONB, nullable=True)  # Store user preferences as JSON
    
    # Relationships
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    final_itineraries = relationship("FinalItinerary", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User(user_id={self.user_id}, username={self.username})>"


class ChatSession(Base):
    """Chat session table to group related messages and interactions"""
    __tablename__ = 'chat_sessions'
    
    chat_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.user_id'), nullable=False)
    session_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    session_metadata = Column(JSONB, nullable=True)  # Store session-specific metadata
    
    # Relationships
    user = relationship("User", back_populates="chat_sessions")
    messages = relationship("Message", back_populates="chat_session", cascade="all, delete-orphan")
    tool_calls = relationship("ToolExecution", back_populates="chat_session", cascade="all, delete-orphan")
    final_itineraries = relationship("FinalItinerary", back_populates="chat_session", cascade="all, delete-orphan")
    researcher_agents = relationship("ResearcherAgent", back_populates="chat_session", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index('idx_chat_sessions_user_id', 'user_id'),
        Index('idx_chat_sessions_created_at', 'created_at'),
    )
    
    def __repr__(self):
        return f"<ChatSession(chat_id={self.chat_id}, user_id={self.user_id})>"


class Message(Base):
    """Messages table to store all chat messages"""
    __tablename__ = 'messages'
    
    message_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.user_id'), nullable=False)
    type = Column(String(50), nullable=False)  # 'human', 'ai', 'system', 'tool'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    message_name = Column(String(255), nullable=True)  # e.g., 'Supervisor_Agent_Response'
    additional_kwargs = Column(JSONB, nullable=True)  # Additional metadata for the message

    
    # Token usage tracking
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    cache_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)
    
    # Relationships
    chat_session = relationship("ChatSession", back_populates="messages")
    user = relationship("User")
    tool_execution = relationship("ToolExecution", back_populates="message", uselist=False)  # One-to-one
    
    # Indexes
    __table_args__ = (
        Index('idx_messages_chat_id', 'chat_id'),
        Index('idx_messages_user_id', 'user_id'),
        Index('idx_messages_created_at', 'created_at'),
    )
    
    def __repr__(self):
        return f"<Message(message_id={self.message_id}, type={self.type})>"


class ToolExecution(Base):
    """Tool execution metadata table to store detailed tool invocation data"""
    __tablename__ = 'tool_executions'
    
    message_id = Column(UUID(as_uuid=True), ForeignKey('messages.message_id'), primary_key=True)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    
    # Tool identification
    tool_name = Column(String(255), nullable=False)
    
    # Tool call details
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    execution_time_ms = Column(Integer, nullable=True)  # Total execution time in milliseconds
    
    # Results
    raw_response = Column(JSONB, nullable=True)  # Raw API response
    unique_identifier = Column(String(255), nullable=True)  # Unique identifier for the tool call
    
    # Relationships
    message = relationship("Message", back_populates="tool_execution")
    chat_session = relationship("ChatSession")
    
    # Indexes
    __table_args__ = (
        Index('idx_tool_executions_message_id', 'message_id'),
        Index('idx_tool_executions_chat_id', 'chat_id'),
        Index('idx_tool_executions_tool_name', 'tool_name'),
    )
    
    def __repr__(self):
        return f"<ToolExecution(execution_id={self.execution_id}, tool_name={self.tool_name}, status={self.execution_status})>"

class FinalItinerary(Base):
    """Final itineraries table to store completed travel plans"""
    __tablename__ = 'final_itineraries'
    
    itinerary_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.user_id'), nullable=False)
    
    # Itinerary details
    trip_name = Column(String(255), nullable=True)
    trip_description = Column(Text, nullable=True)
    itinerary_data = Column(JSONB, nullable=False)  # Complete itinerary structure
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc), nullable=False)
    
    # Trip metadata
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    destination = Column(String(255), nullable=True)
    travelers_count = Column(Integer, nullable=True)
    budget_range = Column(String(100), nullable=True)
    trip_vibe = Column(String(255), nullable=True)
    
    # Status and validation
    is_validated = Column(Boolean, default=False, nullable=False)
    validation_errors = Column(JSONB, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    
    # Unique identifier for sharing/referencing
    unique_identifier = Column(String(255), nullable=True, unique=True)
    
    # Relationships
    chat_session = relationship("ChatSession", back_populates="final_itineraries")
    user = relationship("User", back_populates="final_itineraries")
    
    # Indexes
    __table_args__ = (
        Index('idx_final_itineraries_chat_id', 'chat_id'),
        Index('idx_final_itineraries_user_id', 'user_id'),
        Index('idx_final_itineraries_created_at', 'created_at'),
        Index('idx_final_itineraries_dates', 'start_date', 'end_date'),
        Index('idx_final_itineraries_destination', 'destination'),
    )
    
    def __repr__(self):
        return f"<FinalItinerary(itinerary_id={self.itinerary_id}, trip_name={self.trip_name})>"



class ResearcherAgent(Base):
    """Researcher agents table to store researcher agent execution history and message chains"""
    __tablename__ = 'researcher_agents'
    
    # Primary identifier for this researcher execution
    agent_execution_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Links to parent supervisor message that triggered this researcher
    parent_message_id = Column(UUID(as_uuid=True), nullable=False) # TODO: link this using foreign key currently not possible as this table is populated before messages table
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    
    # Segment identification (multiple researchers can be spawned from same supervisor message)
    segment_number = Column(Integer, nullable=False)  # Unique identifier for this specific segment
    message_name = Column(String(255), nullable=True)  
    type = Column(String(50), nullable=False)  # Type of message (e.g., 'tool', 'human', 'ai', 'system')
    
    # Researcher agent input/configuration
    segment_params = Column(JSONB, nullable=True)  # Complete segment data passed to researcher
    content = Column(Text, nullable=False)  # Content of the researcher agent's message
    # Execution metadata
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    additional_kwargs = Column(JSONB, nullable=True)  # Additional metadata for the researcher agent
    input_tokens = Column(Integer, nullable=True)  # Input tokens used by the researcher agent
    output_tokens = Column(Integer, nullable=True)  # Output tokens generated by the researcher agent
    total_tokens = Column(Integer, nullable=True)  # Total tokens used in the researcher agent's execution
    cache_tokens = Column(Integer, nullable=True)  # Tokens used from cache
    reasoning_tokens = Column(Integer, nullable=True)  # Tokens used for reasoning

    # Relationships
    # parent_message = relationship("Message", foreign_keys=[parent_message_id])
    chat_session = relationship("ChatSession", back_populates="researcher_agents")
    
    # Indexes for efficient querying
    __table_args__ = (
        Index('idx_researcher_agents_parent_message', 'parent_message_id'),
        Index('idx_researcher_agents_chat_id', 'chat_id'),
        Index('idx_researcher_agents_segment', 'parent_message_id', 'segment_number'),
        Index('idx_researcher_agents_created_at', 'created_at'),
    )



# Add any additional indexes for performance optimization
Index('idx_final_itineraries_uid', FinalItinerary.unique_identifier)
