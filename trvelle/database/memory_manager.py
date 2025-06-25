import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from langchain_core.messages import BaseMessage
from ..utils import get_logger

logger = get_logger(__name__)


class InMemoryChatManager:
    """
    Manages in-memory chat history with automatic cleanup of expired entries.
    
    This class provides functionality to:
    - Store chat histories in memory for fast access
    - Track when each chat was last accessed
    - Automatically clean up expired chat histories
    - Provide statistics and manual cleanup options
    """
    
    def __init__(self, 
                 chat_cleanup_interval_minutes: int = 2.5, 
                 chat_expiry_minutes: int = 5):
        """
        Initialize the in-memory chat manager.
        
        Args:
            chat_cleanup_interval_minutes: How often to run cleanup (default: 30 minutes)
            chat_expiry_minutes: How long to keep inactive chats (default: 60 minutes)
        """
        self.chat_history: Dict[str, List[BaseMessage]] = {}
        self.chat_last_accessed: Dict[str, datetime] = {}
        self.chat_cleanup_interval = timedelta(minutes=chat_cleanup_interval_minutes)
        self.chat_expiry_time = timedelta(minutes=chat_expiry_minutes)
        self.last_cleanup = datetime.now()        
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
    
    def start_cleanup_task(self) -> None:
        """Start the background cleanup task."""
        if not self._is_running:
            try:
                # Try to get the current event loop
                loop = asyncio.get_running_loop()
                self._cleanup_task = loop.create_task(self._periodic_cleanup())
                self._is_running = True
                logger.info("Started in-memory chat cleanup task")
            except RuntimeError:
                # No event loop running, will start lazily when needed
                logger.info("No event loop running, cleanup task will start when event loop becomes available")
                self._is_running = False
    
    def _ensure_cleanup_task_started(self) -> None:
        """Ensure cleanup task is started if an event loop is available."""
        if not self._is_running:
            try:
                # Try to get the current event loop
                loop = asyncio.get_running_loop()
                self._cleanup_task = loop.create_task(self._periodic_cleanup())
                self._is_running = True
                logger.info("Started in-memory chat cleanup task (lazy start)")
            except RuntimeError:
                # Still no event loop, will try again later
                pass
    
    async def stop_cleanup_task(self) -> None:
        """Stop the background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._is_running = False
            logger.info("Stopped in-memory chat cleanup task")
    
    def _update_access_time(self, history_key: str) -> None:
        """Update the last access time for a chat history key."""
        self.chat_last_accessed[history_key] = datetime.now()
        logger.debug(f"Updated access time for chat key: {history_key}")
    
    def _cleanup_expired_chats(self) -> int:
        """
        Remove expired chat histories from memory.
        
        Returns:
            Number of chats removed
        """
        current_time = datetime.now()
        expired_keys = []
        
        for history_key, last_accessed in self.chat_last_accessed.items():
            if current_time - last_accessed > self.chat_expiry_time:
                expired_keys.append(history_key)
        
        # Remove expired chats
        for key in expired_keys:
            if key in self.chat_history:
                del self.chat_history[key]
                logger.info(f"Removed expired chat history for key: {key}")
            del self.chat_last_accessed[key]
        
        if expired_keys:
            logger.info(f"Cleaned up {len(expired_keys)} expired chat histories")
        
        return len(expired_keys)
    
    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired chat histories."""
        logger.info("Started periodic chat history cleanup")
        while True:
            try:
                await asyncio.sleep(self.chat_cleanup_interval.total_seconds())
                removed_count = self._cleanup_expired_chats()
                if removed_count > 0:
                    logger.info(f"Periodic cleanup removed {removed_count} expired chats")
            except asyncio.CancelledError:
                logger.info("Chat cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
    
    def get_chat_history(self, history_key: str) -> List[BaseMessage]:
        """
        Get chat history for a given key.
        
        Args:
            history_key: The chat history key
            
        Returns:
            List of messages for the chat
        """
        self._ensure_cleanup_task_started()
        self._update_access_time(history_key)
        return self.chat_history.get(history_key, [])
    
    def set_chat_history(self, history_key: str, messages: List[BaseMessage]) -> None:
        """
        Set chat history for a given key.
        
        Args:
            history_key: The chat history key
            messages: List of messages to store
        """
        self._ensure_cleanup_task_started()
        self.chat_history[history_key] = messages
        self._update_access_time(history_key)
        logger.debug(f"Set chat history for key '{history_key}' with {len(messages)} messages")

    def append_message(self, history_key: str, message: BaseMessage) -> None:
        """
        Append a message to existing chat history.
        
        Args:
            history_key: The chat history key
            message: The message to append
        """
        self._ensure_cleanup_task_started()
        if history_key not in self.chat_history:
            self.chat_history[history_key] = []
        
        self.chat_history[history_key].append(message)
        self._update_access_time(history_key)
        logger.debug(f"Appended message to chat key '{history_key}'")
    
    def extend_messages(self, history_key: str, messages: List[BaseMessage]) -> None:
        """
        Extend chat history with multiple messages.
        
        Args:
            history_key: The chat history key
            messages: List of messages to append
        """
        if history_key not in self.chat_history:
            self.chat_history[history_key] = []
        
        self.chat_history[history_key].extend(messages)
        self._update_access_time(history_key)
        logger.debug(f"Extended chat key '{history_key}' with {len(messages)} messages")
    
    def chat_exists(self, history_key: str) -> bool:
        """
        Check if a chat history exists for the given key.
        
        Args:
            history_key: The chat history key
            
        Returns:
            True if chat exists, False otherwise
        """
        return history_key in self.chat_history
    
    def remove_chat(self, history_key: str) -> bool:
        """
        Remove a specific chat history.
        
        Args:
            history_key: The chat history key to remove
            
        Returns:
            True if chat was removed, False if it didn't exist
        """
        removed = False
        if history_key in self.chat_history:
            del self.chat_history[history_key]
            removed = True
            
        if history_key in self.chat_last_accessed:
            del self.chat_last_accessed[history_key]
            
        if removed:
            logger.info(f"Manually removed chat history for key: {history_key}")
            
        return removed
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """
        Get statistics about current memory usage.
        
        Returns:
            Dictionary with memory statistics
        """
        total_messages = sum(len(history) for history in self.chat_history.values())
        
        return {
            "active_chats": len(self.chat_history),
            "total_messages": total_messages,
            "chat_keys": list(self.chat_history.keys()),
            "last_cleanup": self.last_cleanup.isoformat(),
            "expiry_time_minutes": self.chat_expiry_time.total_seconds() / 60,
            "cleanup_interval_minutes": self.chat_cleanup_interval.total_seconds() / 60,
            "cleanup_task_running": self._is_running
        }
    
    def force_cleanup(self) -> int:
        """
        Manually trigger cleanup and return number of chats removed.
        
        Returns:
            Number of chats removed
        """
        removed_count = self._cleanup_expired_chats()
        logger.info(f"Manual cleanup removed {removed_count} expired chats")
        return removed_count
    
    def clear_all_chats(self) -> int:
        """
        Clear all chat histories from memory.
        
        Returns:
            Number of chats cleared
        """
        count = len(self.chat_history)
        self.chat_history.clear()
        self.chat_last_accessed.clear()
        logger.info(f"Cleared all {count} chat histories from memory")
        return count
    
    def get_chat_info(self, history_key: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific chat.
        
        Args:
            history_key: The chat history key
            
        Returns:
            Dictionary with chat information or None if not found
        """
        if history_key not in self.chat_history:
            return None
            
        return {
            "key": history_key,
            "message_count": len(self.chat_history[history_key]),
            "last_accessed": self.chat_last_accessed.get(history_key),
            "time_since_access": datetime.now() - self.chat_last_accessed.get(history_key, datetime.now())
        }
    
    async def __aenter__(self):
        """Async context manager entry."""
        self.start_cleanup_task()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop_cleanup_task()
