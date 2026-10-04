"""Persistent conversation branches stored alongside the existing messages.

Human messages form a tree. Edits/retries are siblings in the same version
family, and each response belongs to a specific human message. Choosing a
version restores its previously selected descendants without calling a model.
"""
from datetime import datetime, timezone
from .models import ChatSession, Message, PlanningRun, ResearcherAgent


KEY = 'chat_version'


def version(message):
    return (message.additional_kwargs or {}).get(KEY, {})


def settings(chat):
    return dict((chat.session_metadata or {}).get('chat_versions', {}))


def write_settings(chat, value):
    chat.session_metadata = {**(chat.session_metadata or {}), 'chat_versions': value}


def ensure_versions(db, owner, chat_id):
    """Backfill old branches from durable retry records without changing their text."""
    chat = db.query(ChatSession).filter_by(chat_id=chat_id, user_id=owner).with_for_update().first()
    if not chat:
        raise LookupError('Chat not found')
    state = settings(chat)
    if state.get('ready'):
        return chat
    messages = db.query(Message).filter_by(chat_id=chat_id, user_id=owner).order_by(Message.created_at, Message.message_id).all()
    runs = db.query(PlanningRun).filter_by(chat_id=chat_id, user_id=owner).order_by(PlanningRun.created_at).all()
    humans, preferred, seen_runs = {}, {}, set()
    current, active = None, None
    for message in messages:
        identifier = str(message.message_id)
        if message.type == 'human':
            eligible = [run for run in runs if run.created_at.replace(tzinfo=None) <= message.created_at.replace(tzinfo=None)]
            run = eligible[-1] if eligible else None
            target = humans.get((run.request or {}).get('replace_message_id')) if run and str(run.run_id) not in seen_runs else None
            data = {'parent_id': version(target).get('parent_id') if target else current,
                    'root_id': version(target).get('root_id', str(target.message_id)) if target else identifier,
                    'run_id': str(run.run_id) if run else None}
            message.additional_kwargs = {**(message.additional_kwargs or {}), KEY: data}
            humans[identifier] = message
            preferred[data['parent_id'] or 'root'] = identifier
            if run:
                seen_runs.add(str(run.run_id))
            current = identifier
            if not message.superseded:
                active = identifier
        else:
            message.additional_kwargs = {**(message.additional_kwargs or {}), KEY: {'turn_id': current}}
    # Preserve a previously selected older branch instead of selecting by date.
    for message in messages:
        if message.type == 'human' and not message.superseded:
            preferred[version(message).get('parent_id') or 'root'] = str(message.message_id)
    write_settings(chat, {'ready': True, 'active_leaf_id': active, 'preferred_children': preferred})
    db.flush()
    return chat


def human_messages(db, owner, chat_id):
    return {str(row.message_id): row for row in db.query(Message).filter_by(chat_id=chat_id, user_id=owner, type='human').order_by(Message.created_at, Message.message_id)}


def ancestors(humans, leaf):
    chain, seen = [], set()
    while leaf:
        if leaf in seen or leaf not in humans:
            raise ValueError('The saved message version is invalid')
        seen.add(leaf)
        chain.append(leaf)
        leaf = version(humans[leaf]).get('parent_id')
    return list(reversed(chain))


def set_active(db, chat, humans, leaf):
    path = ancestors(humans, leaf)
    selected = set(path)
    for row in db.query(Message).filter_by(chat_id=chat.chat_id, user_id=chat.user_id):
        turn = str(row.message_id) if row.type == 'human' else version(row).get('turn_id')
        row.superseded = bool(turn and turn not in selected)
    state = settings(chat)
    preferred = dict(state.get('preferred_children', {}))
    for identifier in path:
        preferred[version(humans[identifier]).get('parent_id') or 'root'] = identifier
    write_settings(chat, {**state, 'active_leaf_id': leaf, 'preferred_children': preferred})
    chat.updated_at = datetime.now(timezone.utc)
    db.flush()
    return path


def select_version(db, owner, chat_id, message_id):
    chat = ensure_versions(db, owner, chat_id)
    if db.query(PlanningRun).filter(PlanningRun.chat_id == chat_id, PlanningRun.status.in_(['queued', 'running'])).first():
        raise ValueError('Wait for planning to finish before switching versions')
    humans = human_messages(db, owner, chat_id)
    target = str(message_id)
    if target not in humans:
        raise LookupError('Message version not found')
    state = settings(chat)
    leaf, visited = target, set()
    while leaf not in visited:
        visited.add(leaf)
        children = [key for key, row in humans.items() if version(row).get('parent_id') == leaf]
        if not children:
            break
        preferred = state.get('preferred_children', {}).get(leaf)
        leaf = preferred if preferred in children else children[-1]
    set_active(db, chat, humans, leaf)
    return version(humans[leaf]).get('run_id')


def rewind_version(db, owner, chat_id, message_id):
    chat = ensure_versions(db, owner, chat_id)
    humans = human_messages(db, owner, chat_id)
    target = humans.get(str(message_id))
    if not target:
        raise ValueError('Message not found in the selected conversation')
    if target.superseded:
        # A worker can die after rewinding but before saving the new prompt.
        # Repeating that same rewind must not make recovery fail.
        if settings(chat).get('active_leaf_id') == version(target).get('parent_id'):
            return
        raise ValueError('Message not found in the selected conversation')
    set_active(db, chat, humans, version(target).get('parent_id'))


def attach_message(db, message, config):
    if not isinstance(message, Message):
        return  # Researcher conversations have their own history and run scope.
    chat = ensure_versions(db, message.user_id, message.chat_id)
    state = settings(chat)
    run_id = str(config['run_id']) if config.get('run_id') else None
    if message.type == 'human':
        humans = human_messages(db, message.user_id, message.chat_id)
        first_in_run = not run_id or not any(version(row).get('run_id') == run_id for row in humans.values())
        target = humans.get(str(config.get('replace_message_id'))) if first_in_run else None
        identifier = str(message.message_id)
        data = {'parent_id': version(target).get('parent_id') if target else state.get('active_leaf_id'),
                'root_id': version(target).get('root_id', str(target.message_id)) if target else identifier,
                'run_id': run_id}
        preferred = {**state.get('preferred_children', {}), data['parent_id'] or 'root': identifier}
        write_settings(chat, {**state, 'active_leaf_id': identifier, 'preferred_children': preferred})
        config['turn_message_id'] = identifier
    else:
        turn = config.get('turn_message_id')
        if not turn and run_id:
            humans = human_messages(db, message.user_id, message.chat_id)
            turn = next((key for key, row in reversed(list(humans.items())) if version(row).get('run_id') == run_id), None)
        data = {'turn_id': turn or state.get('active_leaf_id')}
    message.additional_kwargs = {**(message.additional_kwargs or {}), KEY: data}


def ordered_active_messages(db, owner, chat_id):
    chat = ensure_versions(db, owner, chat_id)
    humans = human_messages(db, owner, chat_id)
    path = ancestors(humans, settings(chat).get('active_leaf_id'))
    positions = {identifier: index for index, identifier in enumerate(path)}
    rows = db.query(Message).filter_by(chat_id=chat_id, user_id=owner, superseded=False).order_by(Message.created_at, Message.message_id).all()
    return sorted(rows, key=lambda row: (
        positions.get(str(row.message_id) if row.type == 'human' else version(row).get('turn_id'), -1),
        0 if row.type == 'human' else 1, row.created_at, str(row.message_id)))


def navigation_for(humans, turn_id):
    turn = humans.get(turn_id)
    if not turn:
        return None
    siblings = [key for key, row in humans.items() if version(row).get('root_id', key) == version(turn).get('root_id', turn_id)]
    if len(siblings) < 2:
        return None
    return {'current': siblings.index(turn_id) + 1, 'total': len(siblings), 'message_ids': siblings}


def selected_run_id(db, owner, chat_id):
    chat = ensure_versions(db, owner, chat_id)
    leaf = settings(chat).get('active_leaf_id')
    message = db.get(Message, leaf) if leaf else None
    return version(message).get('run_id') if message else None


def search_ids_for_turn(db, owner, chat_id, turn_id):
    """Keep quotes from sibling branches out of an itinerary's alternatives."""
    humans = human_messages(db, owner, chat_id)
    allowed = set(ancestors(humans, turn_id))
    runs = {version(humans[key]).get('run_id') for key in allowed} - {None}
    main = {str(row.message_id) for row in db.query(Message).filter_by(chat_id=chat_id, user_id=owner)
            if version(row).get('turn_id') in allowed}
    research = {str(row.message_id) for row in db.query(ResearcherAgent).filter_by(chat_id=chat_id)
                if row.run_id and str(row.run_id) in runs}
    return main | research
