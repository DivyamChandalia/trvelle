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
    tool_calls = relationship("ToolCall", back_populates="chat_session", cascade="all, delete-orphan")
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
    message_type = Column(String(50), nullable=False)  # 'Human', 'AI', 'System', 'Tool'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    message_order = Column(Integer, nullable=False)  # Order within the chat session
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
    tool_calls = relationship("ToolCall", back_populates="message", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index('idx_messages_chat_id', 'chat_id'),
        Index('idx_messages_user_id', 'user_id'),
        Index('idx_messages_created_at', 'created_at'),
        Index('idx_messages_order', 'chat_id', 'message_order'),
    )
    
    def __repr__(self):
        return f"<Message(message_id={self.message_id}, type={self.message_type})>"


class ToolCall(Base):
    """Tool calls table to store information about tool invocations"""
    __tablename__ = 'tool_calls'
    
    tool_call_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    message_id = Column(UUID(as_uuid=True), ForeignKey('messages.message_id'), nullable=True)
    
    # Tool call details
    tool_name = Column(String(255), nullable=False)
    tool_call_args = Column(JSONB, nullable=False)  # Arguments passed to the tool
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    
    # Execution tracking
    execution_status = Column(String(50), default='pending', nullable=False)  # 'pending', 'success', 'error'
    execution_time_ms = Column(Integer, nullable=True)  # Execution time in milliseconds
    error_message = Column(Text, nullable=True)  # Store error messages if execution fails
    
    # Relationships
    chat_session = relationship("ChatSession", back_populates="tool_calls")
    message = relationship("Message", back_populates="tool_calls")
    tool_responses = relationship("ToolResponse", back_populates="tool_call", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index('idx_tool_calls_chat_id', 'chat_id'),
        Index('idx_tool_calls_tool_name', 'tool_name'),
        Index('idx_tool_calls_created_at', 'created_at'),
        Index('idx_tool_calls_status', 'execution_status'),
    )
    
    def __repr__(self):
        return f"<ToolCall(tool_call_id={self.tool_call_id}, tool_name={self.tool_name})>"


class ToolResponse(Base):
    """Tool responses table to store tool execution results"""
    __tablename__ = 'tool_responses'
    
    response_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tool_call_id = Column(UUID(as_uuid=True), ForeignKey('tool_calls.tool_call_id'), nullable=False)
    
    # Response details
    response_content = Column(Text, nullable=True)  # Main response content
    response_data = Column(JSONB, nullable=True)  # Structured response data
    response_type = Column(String(50), nullable=False)  # 'success', 'error', 'partial'
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    
    # Processing metadata
    processing_time_ms = Column(Integer, nullable=True)
    raw_response = Column(JSONB, nullable=True)  # Store raw API responses for debugging
    parsed_response = Column(JSONB, nullable=True)  # Store parsed/processed responses
    
    # Unique identifiers for external resources
    unique_identifier = Column(String(255), nullable=True)  # e.g., flight UID, hotel UID
    
    # Relationships
    tool_call = relationship("ToolCall", back_populates="tool_responses")
    
    # Indexes
    __table_args__ = (
        Index('idx_tool_responses_tool_call_id', 'tool_call_id'),
        Index('idx_tool_responses_type', 'response_type'),
        Index('idx_tool_responses_created_at', 'created_at'),
        Index('idx_tool_responses_uid', 'unique_identifier'),
    )
    
    def __repr__(self):
        return f"<ToolResponse(response_id={self.response_id}, type={self.response_type})>"


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
    """Researcher agents table to store researcher agent execution history"""
    __tablename__ = 'researcher_agents'
    
    researcher_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id = Column(UUID(as_uuid=True), ForeignKey('chat_sessions.chat_id'), nullable=False)
    
    # Researcher agent details
    segment_number = Column(Integer, nullable=False)
    city = Column(String(255), nullable=False)
    arrival_datetime = Column(DateTime, nullable=True)
    departure_datetime = Column(DateTime, nullable=True)
    adults = Column(Integer, default=1, nullable=False)
    children = Column(Integer, default=0, nullable=False)
    
    # Research parameters
    interests = Column(JSONB, nullable=True)  # List of interests
    information = Column(Text, nullable=True)  # Additional information/context
    query = Column(Text, nullable=True)  # Specific query for research
    hotel_chosen = Column(String(255), nullable=True)  # Chosen hotel UID
    
    # Execution tracking
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    execution_status = Column(String(50), default='pending', nullable=False)  # 'pending', 'running', 'completed', 'failed'
    execution_start_time = Column(DateTime, nullable=True)
    execution_end_time = Column(DateTime, nullable=True)
    execution_duration_ms = Column(Integer, nullable=True)
    
    # Results
    research_results = Column(JSONB, nullable=True)  # Research results data
    completed_segment = Column(JSONB, nullable=True)  # Completed trip segment
    hotels_found = Column(JSONB, nullable=True)  # Hotels found during research
    error_messages = Column(JSONB, nullable=True)  # Any error messages
    
    # LangChain integration
    langchain_tool_call_id = Column(String(255), nullable=True)
    messages_generated = Column(JSONB, nullable=True)  # Messages generated by the researcher
    
    # Relationships
    chat_session = relationship("ChatSession", back_populates="researcher_agents")
    
    # Indexes
    __table_args__ = (
        Index('idx_researcher_agents_chat_id', 'chat_id'),
        Index('idx_researcher_agents_segment', 'segment_number'),
        Index('idx_researcher_agents_city', 'city'),
        Index('idx_researcher_agents_status', 'execution_status'),
        Index('idx_researcher_agents_created_at', 'created_at'),
        Index('idx_researcher_agents_dates', 'arrival_datetime', 'departure_datetime'),
    )
    
    def __repr__(self):
        return f"<ResearcherAgent(researcher_id={self.researcher_id}, city={self.city}, segment={self.segment_number})>"


# Add any additional indexes for performance optimization
Index('idx_final_itineraries_uid', FinalItinerary.unique_identifier)
