from ..utils.secrets import redact_secrets
from .models import Base, User, ChatSession, Message, ToolExecution, ResearcherAgent, ItineraryRevision
from .chat_versions import (attach_message, ordered_active_messages, rewind_version,
    select_version, human_messages, navigation_for, version, search_ids_for_turn, ensure_versions)
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
from sqlalchemy import cast, String, or_
import json
from ..utils.itinerary_edits import hotel_stay_key
import yaml
logger = get_logger(__name__)
load_environment()

class DBHandler:
    create_tables()
    engine = create_engine(os.getenv("DB_URI").replace("postgresql://", "postgresql+psycopg2://", 1))
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
            try:
                return uuid.UUID(cleaned_id)
            except ValueError:
                return uuid.uuid5(uuid.NAMESPACE_URL, message_id)
        elif isinstance(message_id, uuid.UUID):
            # Convert UUID to string in LangChain format
            return message_id
        elif message_id is None:
            # Handle None case
            return uuid.uuid4()
        else:
            raise TypeError("Expected str or UUID input.")

    @classmethod
    def load_chat_history(cls, user_id: UUID, chat_id: UUID, segment_number: int = None, run_id=None) -> List[BaseMessage]:
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

        if segment_number is not None:
            # Load messages for the researcher agent
            query = db.query(ResearcherAgent).filter(
                ResearcherAgent.chat_id == chat_id,
                ResearcherAgent.segment_number == segment_number
            )
            if run_id:
                query = query.filter(ResearcherAgent.run_id == run_id)
            messages = query.order_by(ResearcherAgent.created_at.asc()).all()
        else:
            # Load all messages for this chat
            messages = ordered_active_messages(db, user_id, chat_id)
            db.commit()

        langchain_messages = []
        for message in messages:
            if message.type == "human":
                message = HumanMessage(content=message.content, id=str(message.message_id))
            elif message.type == "ai":
                message = AIMessage(content=(message.additional_kwargs or {}).get("content_blocks", message.content),
                                    id=str(message.message_id),
                                    tool_calls=(message.additional_kwargs or {}).get("tool_calls", []),
                                    additional_kwargs=message.additional_kwargs or {},
                                    name=message.message_name)
            elif message.type == "tool":
                message = ToolMessage(
                    content=message.content,
                    tool_call_id=(message.additional_kwargs or {}).get("tool_call_id", str(message.message_id)),
                    name=message.message_name
                )
            else:
                logger.error(f"Unknown message type: {message.type} for message ID: {message.message_id}")
                continue
            langchain_messages.append(message)

        db.close()
        return langchain_messages

    @classmethod
    def rewind_chat_turn(cls, user_id: UUID, chat_id: UUID, message_id: UUID) -> None:
        """Create a new active branch; retain previous messages, evidence and plans."""
        with cls.db_session() as db:
            try:
                rewind_version(db, user_id, chat_id, message_id)
            except LookupError as error:
                raise ValueError('Message not found') from error
            db.commit()

    @classmethod
    def select_chat_version(cls, user_id: UUID, chat_id: UUID, message_id: UUID):
        with cls.db_session() as db:
            run_id = select_version(db, user_id, chat_id, message_id)
            db.commit()
        return run_id

    @classmethod
    def save_message_to_db(cls, message: BaseMessage, config: Dict[str, Any]) -> None:
        """Save a single message to database."""
        db = cls.db_session()
        msg_to_db = cls.get_message_from_response(message, config)
        if db.get(type(msg_to_db), msg_to_db.message_id) is None:
            attach_message(db, msg_to_db, config)
            db.add(msg_to_db)
        db.query(ChatSession).filter(
            ChatSession.chat_id == config.get("chat_id"),
            ChatSession.user_id == config.get("user_id")
        ).update({"updated_at": datetime.datetime.now(datetime.timezone.utc)})
        db.commit()
        db.close()

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
            'content': response.text if hasattr(response, 'text') else response.content,
            'message_name': response.name if hasattr(response, 'name') else None,
            'type': response.type,
            'additional_kwargs': {**response.additional_kwargs, **({'tool_calls': response.tool_calls} if hasattr(response, 'tool_calls') else {}), **({'content_blocks': response.content} if isinstance(response.content, list) else {}), **({'tool_call_id': response.tool_call_id} if response.type == 'tool' else {})},
            'input_tokens': usage_metadata['input_tokens'],
            'output_tokens': usage_metadata['output_tokens'],
            'total_tokens': usage_metadata['total_tokens'],
            'cache_tokens': usage_metadata.get('input_token_details', {}).get('cache_read'),
            'reasoning_tokens': usage_metadata.get('output_token_details', {}).get('reasoning'),
            'created_at': datetime.datetime.now(datetime.timezone.utc)
        }

        if "researcher_id" in config:
            return ResearcherAgent(
                **common_attrs,
                parent_message_id=cls.get_message_uuid(config["researcher_id"]),
                segment_number=config["segment_number"],
                run_id=config.get('run_id'),
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
            async def wrapper(self, tool_calls: List[Dict[str, Any]], agent_type: str = None, config: Optional[Dict[str, Any]] = None):
                # Execute the function
                if agent_type is None:
                    messages, raw_messages = await func(self, tool_calls, config)
                else:
                    messages, raw_messages = await func(self, tool_calls, agent_type, config)

                # Save the tool messages to the database
                if config:
                    db = cls.db_session()
                    for (message, raw) in zip(messages, raw_messages):
                        msg_to_db = cls.get_message_from_response(message, config)
                        if db.get(type(msg_to_db), msg_to_db.message_id) is None:
                            attach_message(db, msg_to_db, config)
                            db.add(msg_to_db)
                        if raw:
                            if message.name == 'itinerary_tool' and isinstance(raw, dict):
                                allowed = search_ids_for_turn(db, config['user_id'], config['chat_id'], version(msg_to_db).get('turn_id'))
                                raw = {**raw, 'source_search_ids': [str(row.message_id) for row in db.query(ToolExecution).filter(
                                    ToolExecution.chat_id == config['chat_id'], ToolExecution.tool_name.in_(['hotel_search', 'flight_search'])) if str(row.message_id) in allowed]}
                            tool_execution = ToolExecution(
                                chat_id=config.get("chat_id"),
                                message_id=msg_to_db.message_id,
                                tool_name=message.name,
                                raw_response=redact_secrets(raw),
                                created_at=datetime.datetime.now(datetime.timezone.utc)
                            )
                            if message.name == "itinerary_tool":
                                trip_name = raw.get("trip_name", "New Chat")
                                db.query(ChatSession).filter(
                                    ChatSession.chat_id == config.get("chat_id"),
                                    ChatSession.user_id == config.get("user_id")
                                ).update({"session_name": trip_name})
                            if db.get(ToolExecution, tool_execution.message_id) is None:
                                db.add(tool_execution)
                    db.query(ChatSession).filter(
                        ChatSession.chat_id == config.get("chat_id"),
                        ChatSession.user_id == config.get("user_id")
                    ).update({"updated_at": datetime.datetime.now(datetime.timezone.utc)})
                    db.commit()
                    db.close()
                return messages
            return wrapper
        return decorator

    @classmethod
    def get_filtered_chat_history(cls, user_id: UUID, chat_id: UUID) -> List[Dict[str, Any]]:
        """Get chat history with all human messages and all supervisor agent messages."""
        db = cls.db_session()

        try:
            # Get all messages for this chat ordered by creation time
            try:
                messages = ordered_active_messages(db, user_id, chat_id)
            except LookupError:
                return []
            humans = human_messages(db, user_id, chat_id)
            db.commit()

            filtered_messages = []

            for message in messages:
                # Include human messages and supervisor agent messages
                if (message.type == "human" or
                    (message.type == "ai" and message.message_name == "Supervisor_Agent" and bool(message.content)
                     and not (message.additional_kwargs or {}).get('tool_calls'))):

                    filtered_messages.append({
                        "message_id": str(message.message_id),
                        "type": message.type,
                        "content": message.content,
                        "created_at": message.created_at.isoformat(),
                    })
                elif message.type == "tool" and message.message_name == "itinerary_tool" and db.get(ToolExecution, message.message_id):
                    from trvelle.utils.travel_text import itinerary_chat_text
                    filtered_messages.append({
                        "message_id": str(message.message_id), "type": "tool", "content": itinerary_chat_text(db.get(ToolExecution, message.message_id).raw_response or {}),
                        "created_at": message.created_at.isoformat(),
                        "metadata": {"itineraryId": str(message.message_id)},
                    })

            by_id = {str(row.message_id): row for row in messages}
            last_responses = {}
            for item in filtered_messages:
                row = by_id[item['message_id']]
                if row.type != 'human':
                    last_responses[version(row).get('turn_id')] = item['message_id']
            for item in filtered_messages:
                row = by_id[item['message_id']]
                turn_id = str(row.message_id) if row.type == 'human' else version(row).get('turn_id')
                if row.type == 'human' or last_responses.get(turn_id) == item['message_id']:
                    navigation = navigation_for(humans, turn_id)
                    if navigation:
                        item['metadata'] = {**item.get('metadata', {}), 'versions': navigation}
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

    def get_itinerary(self, user_id: UUID, chat_id: UUID, itinerary_id: UUID, revision=None) -> Dict[str, Any]:
        """Retrieve a specific itinerary by ID."""
        db = self.db_session()

        try:
            # Ensure user exists
            existing_user = db.query(User).filter(User.user_id == user_id).first()
            if not existing_user:
                return {"error": "User not found"}

            # Ensure chat session exists
            chat_session = db.query(ChatSession).filter(
                ChatSession.user_id == user_id,
                ChatSession.chat_id == chat_id
            ).first()
            if not chat_session:
                return {"error": "Chat session not found"}

            ensure_versions(db, user_id, chat_id)
            db.commit()

            # Retrieve the itinerary
            itinerary = db.query(ToolExecution).filter(
                ToolExecution.chat_id == chat_id,
                ToolExecution.message_id == itinerary_id
            ).first()

            if not itinerary:
                return {"error": "Itinerary not found"}

            result = dict(itinerary.raw_response or {})
            if revision and revision != result.get('revision', 1):
                saved = db.get(ItineraryRevision, (itinerary_id, revision))
                if not saved or saved.chat_id != chat_id:
                    return {'error': 'Version not found'}
                result = dict(saved.raw)
            result['itinerary_id'] = str(itinerary_id)
            result['latest_revision'] = (itinerary.raw_response or {}).get('revision', 1)
            searches = db.query(ToolExecution).filter(
                ToolExecution.chat_id == chat_id,
                ToolExecution.tool_name.in_(['hotel_search', 'flight_search']),
                or_(ToolExecution.created_at <= itinerary.created_at,
                    ToolExecution.unique_identifier == str(itinerary_id))
            ).order_by(ToolExecution.created_at.desc()).all()
            if 'source_search_ids' in result:
                identifiers = set(result['source_search_ids'])
                searches = [search for search in searches if str(search.message_id) in identifiers]
            saved_message = db.get(Message, itinerary_id)
            turn_id = version(saved_message).get('turn_id') if saved_message else None
            if turn_id:
                allowed = search_ids_for_turn(db, user_id, chat_id, turn_id)
                searches = [search for search in searches if str(search.message_id) in allowed or search.unique_identifier == str(itinerary_id)]
            result['source_search_ids'] = [str(search.message_id) for search in searches]
            hotels, flights, seen = [], [], set()
            description = json.dumps(result, ensure_ascii=False).casefold()
            for search in searches:
                raw = search.raw_response
                if search.tool_name == 'hotel_search' and isinstance(raw, dict):
                    params = raw.get('search_parameters', {})
                    dates = result.get('summary', {}).get('dates', {})
                    if dates.get('start') and params.get('check_in_date') and params['check_in_date'] < dates['start']:
                        continue
                    if dates.get('end') and params.get('check_out_date') and params['check_out_date'] > dates['end']:
                        continue
                    query = params.get('q', '')
                    import math
                    gps = next((h.get('gps_coordinates', {}) for h in raw.get('properties', []) if h.get('gps_coordinates')), {})
                    destination = raw.get('stay_context', {}).get('destination') or (f"{math.floor(gps['latitude'])}:{math.floor(gps['longitude'])}" if 'latitude' in gps and 'longitude' in gps else query)
                    stay_key = hotel_stay_key({**params, 'destination': destination})
                    properties = [hotel for hotel in raw.get('properties', []) if hotel.get('choose_uid')]
                    properties.sort(key=lambda hotel: hotel.get('name', '').casefold() not in description)
                    for hotel in properties:
                        identity = hotel.get('property_token') or hotel.get('name', '').casefold()
                        if ('property', stay_key, identity) in seen:
                            continue
                        seen.add(('property', stay_key, identity))
                        hotel = {**hotel, **result.get('hotel_details', {}).get(hotel['choose_uid'], {})}
                        hotels.append({**hotel, 'location': hotel.get('address') or hotel.get('location') or '', 'destination': destination, 'stay_key': stay_key, 'currency': params.get('currency', 'USD'),
                            'mentioned_in_plan': bool(hotel.get('name')) and hotel['name'].casefold() in description,
                            'check_in_date': params.get('check_in_date'), 'check_out_date': params.get('check_out_date')})
                elif search.tool_name == 'flight_search' and isinstance(raw, list):
                    uid = next((part.get('choose_uid') for part in raw if isinstance(part, dict) and part.get('choose_uid')), None)
                    if not uid or ('flight', uid) in seen:
                        continue
                    seen.add(('flight', uid))
                    raw = result.get('flight_selections', {}).get(uid, raw)
                    legs = []
                    for part in raw:
                        if not isinstance(part, dict):
                            continue
                        options = (part.get('best_flights') or []) + (part.get('other_flights') or [])
                        if options:
                            selected = min(part.get('selected_option_index', 0), len(options) - 1)
                            legs.append({**options[selected], 'currency': options[selected].get('currency') or part.get('search_parameters', {}).get('currency', '')})
                    if legs:
                        flights.append({'uid': uid, 'legs': legs})
            cards = [item for day in result.get('daily_plan', []) for item in day.get('items', [])]
            selected_uids = {item.get('uid') for item in cards if item.get('uid')}
            selected_by_stay = dict(result.get('hotel_selections', {}))
            for hotel in hotels:
                if hotel['choose_uid'] in selected_uids:
                    selected_by_stay.setdefault(hotel['stay_key'], hotel['choose_uid'])
            # A property mentioned in a rejected shortlist or planning note is
            # not a selection. Only hotel cards or an explicit user edit select it.
            for hotel in hotels:
                hotel['selected'] = selected_by_stay.get(hotel['stay_key']) == hotel['choose_uid']
            selected_flight = result.get('selected_flight_uid') or next((f['uid'] for f in flights if f['uid'] in selected_uids), None)
            if not selected_flight and flights:
                selected_flight = flights[0]['uid']
            for flight in flights:
                flight['selected'] = flight['uid'] == selected_flight
            result['travel_options'] = {'hotels': hotels, 'flights': flights}
            import re
            request_text = ' '.join(row.content for row in db.query(Message).filter_by(chat_id=chat_id, user_id=user_id, type='human', superseded=False).all()).casefold()
            requirements = dict(result.get('requirements') or {})
            if requirements.get('private_room') is None and re.search(r'private (?:double |twin |queen )?room', request_text):
                requirements['private_room'] = True
            if requirements.get('bed') is None and re.search(r'double(?:/queen)?(?:[ -]room| bed)|queen(?:[ -]room| bed)', request_text):
                requirements['bed'] = 'double'
            minimum = re.search(r'(?:rated (?:at least )?|rating (?:at least )?)([1-5])(?:/5|\b)', request_text)
            if minimum and requirements.get('minimum_rating') is None:
                requirements['minimum_rating'] = float(minimum[1])
            result['requirements'] = requirements
            result['selected_flight_uid'] = selected_flight
            result.setdefault('revision', 1)
            from trvelle.utils.itinerary_validation import validate_trip
            result['validation'] = validate_trip(result)
            if any(issue['code'] == 'hotel_missing' for issue in result['validation']['issues']):
                result['planning_status'] = 'partial'
            from trvelle.utils.budget import budget_breakdown
            result['budget_breakdown'] = budget_breakdown(result)
            from trvelle.utils.travel_text import readable_trip
            return readable_trip(result)

        finally:
            db.close()

    def save_flight_selection(self, user_id, chat_id, itinerary_id, uid, raw):
        with self.db_session() as db:
            chat = db.query(ChatSession).filter(ChatSession.chat_id == chat_id, ChatSession.user_id == user_id).first()
            itinerary = db.query(ToolExecution).filter(ToolExecution.message_id == itinerary_id, ToolExecution.chat_id == chat_id, ToolExecution.tool_name == 'itinerary_tool').first()
            if not chat or not itinerary:
                raise ValueError('Itinerary not found')
            result = dict(itinerary.raw_response or {})
            result['flight_selections'] = {**result.get('flight_selections', {}), uid: redact_secrets(raw)}
            result.get('detail_reports', {}).pop(f'flight:{uid}', None)
            self._revision(db, itinerary, result, 'flight selection')
            db.commit()
        return self.get_itinerary(user_id, chat_id, itinerary_id)

    def save_itinerary_edit(self, user_id, chat_id, itinerary_id, raw):
        with self.db_session() as db:
            chat = db.query(ChatSession).filter(ChatSession.chat_id == chat_id, ChatSession.user_id == user_id).first()
            itinerary = db.query(ToolExecution).filter(ToolExecution.message_id == itinerary_id, ToolExecution.chat_id == chat_id, ToolExecution.tool_name == 'itinerary_tool').first()
            if not chat or not itinerary:
                raise ValueError('Itinerary not found')
            # Enriched options and display conversions are derived when reading.
            raw = {key:value for key,value in raw.items() if key not in ('travel_options','pricing','budget_breakdown')}
            self._revision(db, itinerary, redact_secrets(raw), 'itinerary edit')
            db.commit()
        return self.get_itinerary(user_id, chat_id, itinerary_id)

    @staticmethod
    def _revision(db, itinerary, updated, reason):
        current = dict(itinerary.raw_response or {})
        revision = current.get('revision', 1)
        if 'source_search_ids' not in current:
            current['source_search_ids'] = [str(row.message_id) for row in db.query(ToolExecution).filter(
                ToolExecution.chat_id == itinerary.chat_id, ToolExecution.tool_name.in_(['hotel_search', 'flight_search']),
                or_(ToolExecution.created_at <= itinerary.created_at, ToolExecution.unique_identifier == str(itinerary.message_id)))]
        updated = {**updated, 'source_search_ids': updated.get('source_search_ids', current['source_search_ids'])}
        if db.get(ItineraryRevision, (itinerary.message_id, revision)) is None:
            db.add(ItineraryRevision(itinerary_id=itinerary.message_id, revision=revision,
                chat_id=itinerary.chat_id, raw=current, reason='saved version'))
        updated = {**updated, 'revision': revision + 1}
        db.add(ItineraryRevision(itinerary_id=itinerary.message_id, revision=revision + 1,
            chat_id=itinerary.chat_id, raw=updated, reason=reason))
        itinerary.raw_response = updated

    def itinerary_search_issue(self, user_id: UUID, chat_id: UUID, itinerary: Dict[str, Any]) -> Optional[str]:
        """Reject broken search references before presenting an itinerary as complete."""
        with self.db_session() as db:
            if not db.query(ChatSession).filter_by(user_id=user_id, chat_id=chat_id).first():
                return 'Chat not found'
            results = db.query(ToolExecution).filter(ToolExecution.chat_id == chat_id,
                ToolExecution.tool_name.in_(['flight_search', 'hotel_search'])).all()
            chat = ensure_versions(db, user_id, chat_id)
            leaf = (chat.session_metadata or {}).get('chat_versions', {}).get('active_leaf_id')
            if leaf:
                allowed = search_ids_for_turn(db, user_id, chat_id, leaf)
                known = {str(row.message_id) for row in db.query(Message).filter_by(chat_id=chat_id)} | {str(row.message_id) for row in db.query(ResearcherAgent).filter_by(chat_id=chat_id)}
                results = [row for row in results if str(row.message_id) in allowed or str(row.message_id) not in known]
            successful_flights = [row for row in results if row.tool_name == 'flight_search' and isinstance(row.raw_response, list)]
            attempted_flights = db.query(Message).filter_by(chat_id=chat_id, message_name='flight_search', superseded=False).first() is not None
            latest_user = db.query(Message).filter_by(chat_id=chat_id, type='human', superseded=False).order_by(Message.created_at.desc()).first()
            skip_flights = latest_user and any(phrase in latest_user.content.casefold() for phrase in ('without flights', 'skip flights', 'no flights'))
            partial = itinerary.get('planning_status') == 'partial' and bool(itinerary.get('unfinished'))
            if attempted_flights and not successful_flights and not skip_flights and not partial:
                return 'Flight searches failed. Retry flight_search with valid future dates before creating the itinerary, or explain the failure and ask whether the user wants a plan without flights.'
            available = {'flight': set(), 'hotel': set()}
            for row in results:
                raw = row.raw_response
                if row.tool_name == 'flight_search' and isinstance(raw, list):
                    available['flight'].update(part['choose_uid'] for part in raw if isinstance(part, dict) and part.get('choose_uid'))
                elif row.tool_name == 'hotel_search' and isinstance(raw, dict):
                    available['hotel'].update(hotel['choose_uid'] for hotel in raw.get('properties', []) if hotel.get('choose_uid'))
            dates = itinerary.get('summary', {}).get('dates', {})
            overnight = len(itinerary.get('daily_plan', [])) > 1 or (dates.get('start') and dates.get('end') and str(dates['end']) > str(dates['start']))
            request = latest_user.content.casefold() if latest_user else ''
            skip_hotels = any(phrase in request for phrase in ('no hotels', 'skip hotels', 'without hotels', 'already booked accommodation', 'hotel is already booked', 'staying with family', 'staying with friends'))
            if overnight and not skip_hotels and not partial:
                if not available['hotel']:
                    return 'Overnight itineraries require real hotel searches. Call researcher_agent for the destination and have it run hotel_search for the actual stay dates before finalizing. Web-search hotel names are not hotel search results.'
                if not any(item.get('uid') in available['hotel'] for day in itinerary.get('daily_plan', []) for item in day.get('items', [])):
                    return 'Include a selected hotel card using the exact choose_uid from hotel_search before finalizing this overnight itinerary.'
            for day in itinerary.get('daily_plan', []):
                for item in day.get('items', []):
                    kind = item.get('card_type')
                    if kind in available and item.get('uid') not in available[kind]:
                        return f'Use a real {kind} search result and its exact choose_uid for every {kind} card. Do not invent or omit the UID.'
                    if kind == 'hotel' and dates.get('start') and dates.get('end'):
                        matched = [row.raw_response.get('search_parameters', {}) for row in results
                            if row.tool_name == 'hotel_search' and isinstance(row.raw_response, dict)
                            and any(h.get('choose_uid') == item.get('uid') for h in row.raw_response.get('properties', []))]
                        if matched and not any((not p.get('check_in_date') or p['check_in_date'] >= dates['start'])
                            and (not p.get('check_out_date') or p['check_out_date'] <= dates['end']) for p in matched):
                            return 'The selected hotel was searched for different dates. Search hotels for this trip before finalizing.'
        return None

    def get_tool_response(self, user_id: UUID, chat_id: UUID, human_id: str) -> Dict[str, Any]:
        """Retrieve a specific tool response by ID."""
        db = self.db_session()

        try:
            # Ensure user exists
            existing_user = db.query(User).filter(User.user_id == user_id).first()
            if not existing_user:
                return {"error": "User not found"}

            # Ensure chat session exists
            chat_session = db.query(ChatSession).filter(
                ChatSession.user_id == user_id,
                ChatSession.chat_id == chat_id
            ).first()
            if not chat_session:
                return {"error": "Chat session not found"}

            # Retrieve the tool execution
            tool_execution = None
            for candidate in db.query(ToolExecution).filter_by(chat_id=chat_id).order_by(ToolExecution.created_at.desc()):
                raw = candidate.raw_response
                parts = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
                if str(candidate.message_id) == human_id or any(part.get('choose_uid') == human_id
                    or any(hotel.get('choose_uid') == human_id for hotel in part.get('properties', []))
                    for part in parts if isinstance(part, dict)):
                    tool_execution = candidate
                    break

            if not tool_execution:
                return {"error": "Tool response not found"}

            return redact_secrets(tool_execution.raw_response)

        finally:
            db.close()

    @classmethod
    def delete_chat(cls, user_id: UUID, chat_id: UUID) -> Dict[str, Any]:
        """Delete a chat session and all associated data for a given user."""
        db = cls.db_session()

        try:
            # Verify user exists
            existing_user = db.query(User).filter(User.user_id == user_id).first()
            if not existing_user:
                return {"error": "User not found"}

            # Verify chat session exists and belongs to the user
            chat_session = db.query(ChatSession).filter(
                ChatSession.user_id == user_id,
                ChatSession.chat_id == chat_id
            ).first()

            if not chat_session:
                return {"error": "Chat session not found"}

            # Delete the chat session (cascade will handle related records)
            # This will automatically delete:
            # - All messages in the chat
            # - All tool executions in the chat
            # - All researcher agent records in the chat
            db.delete(chat_session)
            db.commit()

            logger.info(f"Successfully deleted chat session {chat_id} for user {user_id}")
            return {
                "success": True,
                "message": f"Chat session {chat_id} and all associated data deleted successfully"
            }

        except Exception as e:
            db.rollback()
            logger.error(f"Error deleting chat session {chat_id} for user {user_id}: {str(e)}")
            return {"error": f"Failed to delete chat session: {str(e)}"}

        finally:
            db.close()
