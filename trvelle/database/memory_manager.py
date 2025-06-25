import asyncio
import threading
import weakref
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from langchain_core.messages import BaseMessage
from ..utils import get_logger

logger = get_logger(__name__)


class InMemoryChatManager:
    """
    Thread-safe in-memory chat history manager with automatic cleanup.
    
    Features:
    - Thread-safe operations using RLock
    - Automatic cleanup of expired chat histories
    - Lazy initialization of cleanup task
    - Proper resource management with context manager support
    - Statistics and manual cleanup options
    """
    
    # Class-level registry to track all instances for proper cleanup
    _instances = weakref.WeakSet()
    
    def __init__(self, 
                 chat_cleanup_interval_minutes: float = 5.0, 
                 chat_expiry_minutes: float = 10.0):
        """
        Initialize the in-memory chat manager.
        
        Args:
            chat_cleanup_interval_minutes: How often to run cleanup (default: 30 minutes)
            chat_expiry_minutes: How long to keep inactive chats (default: 60 minutes)
        """
        # Validate inputs
        if chat_cleanup_interval_minutes <= 0 or chat_expiry_minutes <= 0:
            raise ValueError("Cleanup interval and expiry time must be positive")
        
        self._chat_history: Dict[str, List[BaseMessage]] = {}
        self._chat_last_accessed: Dict[str, datetime] = {}
        self._chat_cleanup_interval = timedelta(minutes=chat_cleanup_interval_minutes)
        self._chat_expiry_time = timedelta(minutes=chat_expiry_minutes)
        
        # Thread safety
        self._lock = threading.RLock()
        
        # Cleanup task management
        self._cleanup_task: Optional[asyncio.Task] = None
        self._logging_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._shutdown_event = None  # Will be created when needed
        
        # Add to instance registry
        self._instances.add(self)
        
        logger.info(f"Initialized InMemoryChatManager with cleanup interval: {chat_cleanup_interval_minutes}m, "
                   f"expiry time: {chat_expiry_minutes}m")
        # Don't call logger_log() here - it will be started with the cleanup task
        
    async def logger_log(self):
        """Log the current state of the chat manager."""
        logger.info("Started chat manager logging task")
        
        try:
            while not self._shutdown_event.is_set():
                stats = self.get_memory_stats()
                logger.info(f"Chat Manager Stats: {stats}")
                
                # Sleep for 30 seconds or until shutdown
                try:
                    await asyncio.sleep(self._chat_cleanup_interval.total_seconds())
                except asyncio.CancelledError:
                    logger.info("Chat logging task cancelled during sleep")
                    break
                    
                # Check if shutdown was requested during sleep
                if self._shutdown_event.is_set():
                    break
                    
        except asyncio.CancelledError:
            logger.info("Chat logging task cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in logging loop: {e}")
        finally:
            logger.info("Chat logging task finished")
    
    def start_cleanup_task(self) -> None:
        """Start the background cleanup task if an event loop is available."""
        with self._lock:
            if self._is_running:
                logger.debug("Cleanup task already running")
                return
                
            try:
                loop = asyncio.get_running_loop()
                # Create shutdown event in the correct loop context
                self._shutdown_event = asyncio.Event()
                self._cleanup_task = loop.create_task(self._periodic_cleanup())
                self._logging_task = loop.create_task(self.logger_log())
                self._is_running = True
                logger.info("Started background chat cleanup and logging tasks")
            except RuntimeError:
                logger.debug("No event loop running, cleanup task will start lazily")
    
    def _ensure_cleanup_task_started(self) -> None:
        """Ensure cleanup task is started if an event loop is available."""
        if not self._is_running:
            try:
                loop = asyncio.get_running_loop()
                with self._lock:
                    if not self._is_running:  # Double-check pattern
                        # Create shutdown event in the correct loop context
                        self._shutdown_event = asyncio.Event()
                        self._cleanup_task = loop.create_task(self._periodic_cleanup())
                        self._logging_task = loop.create_task(self.logger_log())
                        self._is_running = True
                        logger.info("Started background chat cleanup and logging tasks (lazy initialization)")
            except RuntimeError:
                pass  # No event loop available yet
    
    async def stop_cleanup_task(self) -> None:
        """Stop the background cleanup task gracefully."""
        with self._lock:
            if not self._is_running:
                return
                
            # Signal shutdown first
            if self._shutdown_event:
                self._shutdown_event.set()
            
            tasks_to_stop = []
            if self._cleanup_task:
                tasks_to_stop.append(("cleanup", self._cleanup_task))
            if self._logging_task:
                tasks_to_stop.append(("logging", self._logging_task))
            
            for task_name, task in tasks_to_stop:
                try:
                    # Wait for graceful shutdown first
                    await asyncio.wait_for(task, timeout=2.0)
                except asyncio.TimeoutError:
                    # Force cancel if graceful shutdown takes too long
                    logger.warning(f"{task_name} task didn't stop gracefully, forcing cancellation")
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                except asyncio.CancelledError:
                    pass  # Expected when cancelling
                except Exception as e:
                    logger.error(f"Error stopping {task_name} task: {e}")
            
            self._is_running = False
            self._cleanup_task = None
            self._logging_task = None
            # Reset shutdown event for potential restart
            self._shutdown_event = None
            logger.info("Stopped background chat cleanup and logging tasks")
    
    def _update_access_time(self, history_key: str) -> None:
        """Update the last access time for a chat history key."""
        with self._lock:
            self._chat_last_accessed[history_key] = datetime.now()
            logger.debug(f"Updated access time for chat key: {history_key}")
    
    def _cleanup_expired_chats(self) -> int:
        """
        Remove expired chat histories from memory.
        
        Returns:
            Number of chats removed
        """
        current_time = datetime.now()
        expired_keys = []
        
        with self._lock:
            # Find expired keys
            for history_key, last_accessed in self._chat_last_accessed.items():
                if current_time - last_accessed > self._chat_expiry_time:
                    expired_keys.append(history_key)
            
            # Remove expired chats
            for key in expired_keys:
                self._chat_history.pop(key, None)
                self._chat_last_accessed.pop(key, None)
        
        if expired_keys:
            logger.info(f"Cleaned up {len(expired_keys)} expired chat histories: {expired_keys}")
        
        return len(expired_keys)
    
    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired chat histories."""
        logger.info("Started periodic chat history cleanup")
        
        try:
            while not self._shutdown_event.is_set():
                # Sleep for the cleanup interval
                try:
                    await asyncio.sleep(self._chat_cleanup_interval.total_seconds())
                except asyncio.CancelledError:
                    logger.info("Chat cleanup task cancelled during sleep")
                    break
                
                # Check if shutdown was requested during sleep
                if self._shutdown_event.is_set():
                    break
                
                # Perform cleanup
                try:
                    removed_count = self._cleanup_expired_chats()
                    logger.debug(f"Periodic cleanup check completed - removed {removed_count} expired chats")
                    if removed_count > 0:
                        logger.info(f"Periodic cleanup removed {removed_count} expired chats")
                except Exception as e:
                    logger.error(f"Error during periodic cleanup: {e}")
                    
        except asyncio.CancelledError:
            logger.info("Chat cleanup task cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in periodic cleanup loop: {e}")
        finally:
            logger.info("Periodic cleanup task finished")
    
    def get_chat_history(self, history_key: str) -> List[BaseMessage]:
        """
        Get chat history for a given key.
        
        Args:
            history_key: The chat history key
            
        Returns:
            List of messages for the chat (copy to prevent external modification)
        """
        self._ensure_cleanup_task_started()
        
        with self._lock:
            self._update_access_time(history_key)
            return self._chat_history.get(history_key, []).copy()
    
    def set_chat_history(self, history_key: str, messages: List[BaseMessage]) -> None:
        """
        Set chat history for a given key.
        
        Args:
            history_key: The chat history key
            messages: List of messages to store
        """
        if not isinstance(messages, list):
            raise TypeError("Messages must be a list")
            
        self._ensure_cleanup_task_started()
        
        with self._lock:
            self._chat_history[history_key] = messages.copy()
            self._update_access_time(history_key)
            logger.debug(f"Set chat history for key '{history_key}' with {len(messages)} messages")

    def append_message(self, history_key: str, message: BaseMessage) -> None:
        """
        Append a message to existing chat history.
        
        Args:
            history_key: The chat history key
            message: The message to append
        """
        if not isinstance(message, BaseMessage):
            raise TypeError("Message must be a BaseMessage instance")
            
        self._ensure_cleanup_task_started()
        
        with self._lock:
            if history_key not in self._chat_history:
                self._chat_history[history_key] = []
            
            self._chat_history[history_key].append(message)
            self._update_access_time(history_key)
            logger.debug(f"Appended message to chat key '{history_key}'")
    
    def extend_messages(self, history_key: str, messages: List[BaseMessage]) -> None:
        """
        Extend chat history with multiple messages.
        
        Args:
            history_key: The chat history key
            messages: List of messages to append
        """
        if not isinstance(messages, list):
            raise TypeError("Messages must be a list")
        if not all(isinstance(msg, BaseMessage) for msg in messages):
            raise TypeError("All messages must be BaseMessage instances")
            
        self._ensure_cleanup_task_started()
        
        with self._lock:
            if history_key not in self._chat_history:
                self._chat_history[history_key] = []
            
            self._chat_history[history_key].extend(messages)
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
        with self._lock:
            return history_key in self._chat_history
    
    def remove_chat(self, history_key: str) -> bool:
        """
        Remove a specific chat history.
        
        Args:
            history_key: The chat history key to remove
            
        Returns:
            True if chat was removed, False if it didn't exist
        """
        with self._lock:
            chat_existed = history_key in self._chat_history
            self._chat_history.pop(history_key, None)
            self._chat_last_accessed.pop(history_key, None)
            
            if chat_existed:
                logger.info(f"Manually removed chat history for key: {history_key}")
                
            return chat_existed
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """
        Get statistics about current memory usage.
        
        Returns:
            Dictionary with memory statistics
        """
        with self._lock:
            total_messages = sum(len(history) for history in self._chat_history.values())
            oldest_access = min(self._chat_last_accessed.values()) if self._chat_last_accessed else None
            
            return {
                "active_chats": len(self._chat_history),
                "total_messages": total_messages,
                "average_messages_per_chat": total_messages / len(self._chat_history) if self._chat_history else 0,
                "chat_keys": list(self._chat_history.keys()),
                "oldest_access": oldest_access.isoformat() if oldest_access else None,
                "expiry_time_minutes": self._chat_expiry_time.total_seconds() / 60,
                "cleanup_interval_minutes": self._chat_cleanup_interval.total_seconds() / 60,
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
        with self._lock:
            count = len(self._chat_history)
            self._chat_history.clear()
            self._chat_last_accessed.clear()
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
        with self._lock:
            if history_key not in self._chat_history:
                return None
                
            last_accessed = self._chat_last_accessed.get(history_key, datetime.now())
            return {
                "key": history_key,
                "message_count": len(self._chat_history[history_key]),
                "last_accessed": last_accessed.isoformat(),
                "time_since_access_seconds": (datetime.now() - last_accessed).total_seconds()
            }
    
    async def __aenter__(self):
        """Async context manager entry."""
        self.start_cleanup_task()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop_cleanup_task()
    
    def __del__(self):
        """Cleanup when object is destroyed."""
        # Note: This is best effort cleanup, proper cleanup should use context manager
        if self._is_running and self._cleanup_task and not self._cleanup_task.done():
            logger.warning("InMemoryChatManager destroyed with running cleanup task. "
                         "Use async context manager for proper cleanup.")
    
    @classmethod
    async def shutdown_all(cls):
        """Class method to shutdown all active instances."""
        instances = list(cls._instances)
        logger.info(f"Shutting down {len(instances)} InMemoryChatManager instances")
        
        for instance in instances:
            try:
                await instance.stop_cleanup_task()
            except Exception as e:
                logger.error(f"Error shutting down instance: {e}")
        
        logger.info("All InMemoryChatManager instances shut down")
    
    # Testing and debugging methods
    def _add_test_chat(self, history_key: str, messages: List[BaseMessage], 
                      access_time: Optional[datetime] = None) -> None:
        """Add a test chat with specific access time (for testing purposes only)."""
        with self._lock:
            self._chat_history[history_key] = messages
            self._chat_last_accessed[history_key] = access_time or datetime.now()
    
    def get_all_chat_keys_with_access_times(self) -> Dict[str, datetime]:
        """Get all chat keys with their access times (for debugging)."""
        with self._lock:
            return self._chat_last_accessed.copy()


if __name__ == "__main__":
    import asyncio
    from langchain_core.messages import HumanMessage
    print("Starting InMemoryChatManager test...")
    async def test_cleanup():
        """Test the automatic cleanup functionality."""
        print("Testing InMemoryChatManager automatic cleanup...")
        
        # Create manager with very short intervals for testing
        chat_manager = InMemoryChatManager(
            chat_cleanup_interval_minutes=0.1,  # 6 seconds
            chat_expiry_minutes=0.05  # 3 seconds
        )
        
        try:
            # Start the cleanup task
            chat_manager.start_cleanup_task()
            print("Started cleanup task")
            
            # Add test messages
            message1 = HumanMessage(content="test message 1")
            message2 = HumanMessage(content="test message 2")
            
            chat_manager.append_message("test_chat_1", message1)
            chat_manager.append_message("test_chat_2", message2)
            
            print(f"Initial state: {chat_manager.get_memory_stats()['active_chats']} active chats")
            print(f"Chat keys: {list(chat_manager.get_all_chat_keys_with_access_times().keys())}")
            
            # Add an old chat that should be cleaned up immediately
            old_time = datetime.now() - timedelta(minutes=1)  # 1 minute ago
            old_message = HumanMessage(content="old test message")
            chat_manager._add_test_chat("old_chat", [old_message], old_time)
            
            print(f"After adding old chat: {chat_manager.get_memory_stats()['active_chats']} active chats")
            
            # Force cleanup to see immediate effect
            removed = chat_manager.force_cleanup()
            print(f"Force cleanup removed: {removed} chats")
            print(f"After force cleanup: {chat_manager.get_memory_stats()['active_chats']} active chats")
            
            # Wait for automatic cleanup cycle
            print("Waiting 8 seconds for automatic cleanup...")
            await asyncio.sleep(8)
            
            stats = chat_manager.get_memory_stats()
            print(f"After automatic cleanup: {stats['active_chats']} active chats")
            print(f"Remaining chat keys: {stats['chat_keys']}")
            
            # Test accessing a chat to refresh its access time
            print("\nTesting access time refresh...")
            chat_manager.append_message("fresh_chat", HumanMessage(content="fresh message"))
            print(f"Added fresh chat: {chat_manager.get_memory_stats()['active_chats']} active chats")
            
            # Wait half the expiry time
            await asyncio.sleep(2)
            
            # Access the chat to refresh it
            history = chat_manager.get_chat_history("fresh_chat")
            print(f"Accessed fresh_chat, got {len(history)} messages")
            
            # Wait for cleanup cycle
            await asyncio.sleep(6)
            
            final_stats = chat_manager.get_memory_stats()
            print(f"Final state: {final_stats['active_chats']} active chats")
            print(f"Final chat keys: {final_stats['chat_keys']}")
            
            print("\nTest completed successfully!")
            
        except Exception as e:
            print(f"Test failed with error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Cleanup
            await chat_manager.stop_cleanup_task()
            print("Stopped cleanup task")
    
    # Run the test
    print("Starting InMemoryChatManager test...")
    asyncio.run(test_cleanup())