"""
PostgreSQL Database Models for PlanIt Travel Assistant

This module contains all the database models for storing user interactions,
chat sessions, messages, tool executions
and researcher agent history.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, JSON, ForeignKey, 
    Boolean, Float, create_engine, Index, text
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    preferences = Column(JSONB, nullable=True)  # Store user preferences as JSON
    
    # Relationships
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User(user_id={self.user_id}, username={self.username})>"


class ChatSession(Base):
    """Chat session table to group related messages and interactions"""
    __tablename__ = 'chat_sessions'
    
    chat_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.user_id'), nullable=False)
    session_name = Column(String(255), nullable=True, default="New Chat")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    session_metadata = Column(JSONB, nullable=True)  # Store session-specific metadata
    
    # Relationships
    user = relationship("User", back_populates="chat_sessions")
    messages = relationship("Message", back_populates="chat_session", cascade="all, delete-orphan")
    tool_calls = relationship("ToolExecution", back_populates="chat_session", cascade="all, delete-orphan")
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    message_name = Column(String(255), nullable=True)  # e.g., 'Supervisor_Agent_Response'
    additional_kwargs = Column(JSONB, nullable=True)  # Additional metadata for the message
    superseded = Column(Boolean, default=False, nullable=False)

    
    # Token usage tracking
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    cache_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)
    
    # Relationships
    chat_session = relationship("ChatSession", back_populates="messages")
    user = relationship("User")
    
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
    
    message_id = Column(UUID(as_uuid=True), primary_key=True)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    
    # Tool identification
    tool_name = Column(String(255), nullable=False)
    
    # Tool call details
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    execution_time_ms = Column(Integer, nullable=True)  # Total execution time in milliseconds
    
    # Results
    raw_response = Column(JSONB, nullable=True)  # Raw API response
    unique_identifier = Column(String(255), nullable=True)  # Unique identifier for the tool call
    
    # Relationships
    chat_session = relationship("ChatSession")
    
    # Indexes
    __table_args__ = (
        Index('idx_tool_executions_message_id', 'message_id'),
        Index('idx_tool_executions_chat_id', 'chat_id'),
        Index('idx_tool_executions_tool_name', 'tool_name'),
    )
    
    def __repr__(self):
        return f"<ToolExecution(execution_id={self.message_id}, tool_name={self.tool_name})>"


class ResearcherAgent(Base):
    """Researcher agents table to store researcher agent execution history and message chains"""
    __tablename__ = 'researcher_agents'
    
    # Primary identifier for this researcher execution
    message_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    additional_kwargs = Column(JSONB, nullable=True)  # Additional metadata for the researcher agent
    run_id = Column(UUID(as_uuid=True), nullable=True)
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


class PlanningRun(Base):
    __tablename__ = 'planning_runs'
    run_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(30), nullable=False, default='queued')
    phase = Column(String(30), nullable=False, default='clarify')
    request = Column(JSONB, nullable=False)
    checkpoint = Column(JSONB, nullable=False, default=dict)
    counters = Column(JSONB, nullable=False, default=dict)
    steering = Column(JSONB, nullable=False, default=list)
    control = Column(String(30), nullable=True)
    active_seconds = Column(Float, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=0)
    last_event_id = Column(Integer, nullable=False, default=0)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    worker_id = Column(String(100), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (Index('one_active_run_per_chat', 'chat_id', unique=True,
        postgresql_where=text("status IN ('queued', 'running')")),)


class RunEvent(Base):
    __tablename__ = 'planning_events'
    run_id = Column(UUID(as_uuid=True), ForeignKey('planning_runs.run_id', ondelete='CASCADE'), primary_key=True)
    event_id = Column(Integer, primary_key=True)
    kind = Column(String(40), nullable=False)
    data = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class RunOperation(Base):
    __tablename__ = 'planning_operations'
    run_id = Column(UUID(as_uuid=True), ForeignKey('planning_runs.run_id', ondelete='CASCADE'), primary_key=True)
    operation_id = Column(String(200), primary_key=True)
    kind = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default='pending')
    result = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SearchCache(Base):
    __tablename__ = 'search_cache'
    cache_key = Column(String(64), primary_key=True)
    provider = Column(String(20), nullable=False)
    scope = Column(String(64), nullable=False)
    query = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False)
    result = Column(JSONB, nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)


class SearchAccount(Base):
    __tablename__ = 'search_accounts'
    scope = Column(String(64), primary_key=True)
    period = Column(String(40), primary_key=True)
    provider = Column(String(20), nullable=False)
    used = Column(Integer, nullable=False, default=0)
    limit = Column(Integer, nullable=False)
    reset_at = Column(DateTime(timezone=True), nullable=False)
    checked_at = Column(DateTime(timezone=True), nullable=True)


class SearchCharge(Base):
    __tablename__ = 'search_charges'
    charge_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    provider = Column(String(20), nullable=False)
    scope = Column(String(64), nullable=False)
    credits = Column(Integer, nullable=False)
    cache_key = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default='reserved')
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SearchProviderState(Base):
    __tablename__ = 'search_provider_states'
    scope = Column(String(64), primary_key=True)
    provider = Column(String(20), nullable=False)
    next_request_at = Column(DateTime(timezone=True), nullable=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    quota = Column(JSONB, nullable=True)


class ItineraryRevision(Base):
    __tablename__ = 'itinerary_revisions'
    itinerary_id = Column(UUID(as_uuid=True), primary_key=True)
    revision = Column(Integer, primary_key=True)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id', ondelete='CASCADE'), nullable=False, index=True)
    raw = Column(JSONB, nullable=False)
    reason = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class FXSnapshot(Base):
    __tablename__ = 'itinerary_fx_snapshots'
    itinerary_id = Column(UUID(as_uuid=True), primary_key=True)
    revision = Column(Integer, primary_key=True)
    target = Column(String(3), primary_key=True)
    rates = Column(JSONB, nullable=False)
