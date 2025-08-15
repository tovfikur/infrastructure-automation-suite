"""
Thread-safe queue system for MCP Distributed Monitoring System.
Handles pending errors, fix processing, and retry queues with priority support.
"""

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Callable, Union
import structlog

logger = structlog.get_logger(__name__)


class QueuePriority(Enum):
    """Priority levels for queue items."""
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


class QueueItemStatus(Enum):
    """Status of items in queues."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class QueueItem:
    """Base class for queue items."""
    id: str
    priority: QueuePriority
    created_at: datetime = field(default_factory=datetime.now)
    status: QueueItemStatus = QueueItemStatus.PENDING
    retry_count: int = 0
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorQueueItem(QueueItem):
    """Queue item for error processing."""
    error_message: str
    context: str
    client_id: str
    service_type: str
    log_source: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class FixQueueItem(QueueItem):
    """Queue item for fix execution."""
    fix_commands: List[str]
    rollback_commands: List[str]
    validation_commands: List[str]
    target_client_id: str
    requires_restart: bool = False
    estimated_downtime: int = 0
    risk_level: str = "medium"
    original_error_id: str = ""


class ThreadSafeQueue:
    """Generic thread-safe priority queue with retry mechanism."""
    
    def __init__(
        self,
        name: str,
        max_size: int = 10000,
        max_retries: int = 3,
        retry_delay_base: float = 1.0,
        cleanup_interval: int = 300  # 5 minutes
    ):
        self.name = name
        self.max_size = max_size
        self.max_retries = max_retries
        self.retry_delay_base = retry_delay_base
        self.cleanup_interval = cleanup_interval
        
        # Priority queues for different priority levels
        self._queues = {
            priority: deque() for priority in QueuePriority
        }
        
        # Processing tracking
        self._processing = {}  # item_id -> QueueItem
        self._completed = deque(maxlen=1000)  # Keep last 1000 completed items
        self._failed = deque(maxlen=1000)    # Keep last 1000 failed items
        
        # Thread safety
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        
        # Async support
        self._async_lock = asyncio.Lock()
        
        # Statistics
        self._stats = {
            "total_enqueued": 0,
            "total_processed": 0,
            "total_failed": 0,
            "current_size": 0,
            "processing_count": 0
        }
        
        # Cleanup timer
        self._cleanup_task = None
        self._start_cleanup_task()
    
    def _start_cleanup_task(self):
        """Start the cleanup task for removing old items."""
        def cleanup_worker():
            while True:
                try:
                    time.sleep(self.cleanup_interval)
                    self._cleanup_old_items()
                except Exception as e:
                    logger.error("Error in cleanup task", queue_name=self.name, error=str(e))
        
        cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        cleanup_thread.start()
    
    def _cleanup_old_items(self):
        """Remove old completed and failed items."""
        cutoff_time = datetime.now() - timedelta(hours=24)
        
        with self._lock:
            # Clean completed items
            while self._completed and self._completed[0].created_at < cutoff_time:
                self._completed.popleft()
            
            # Clean failed items
            while self._failed and self._failed[0].created_at < cutoff_time:
                self._failed.popleft()
    
    def enqueue(self, item: QueueItem) -> bool:
        """Add item to queue. Returns True if successful."""
        with self._lock:
            if self._stats["current_size"] >= self.max_size:
                logger.warning(
                    "Queue full, rejecting item",
                    queue_name=self.name,
                    max_size=self.max_size,
                    item_id=item.id
                )
                return False
            
            # Add to appropriate priority queue
            self._queues[item.priority].append(item)
            self._stats["total_enqueued"] += 1
            self._stats["current_size"] += 1
            
            logger.info(
                "Item enqueued",
                queue_name=self.name,
                item_id=item.id,
                priority=item.priority.name,
                current_size=self._stats["current_size"]
            )
            
            # Notify waiting threads
            self._condition.notify_all()
            
            return True
    
    def dequeue(self, timeout: Optional[float] = None) -> Optional[QueueItem]:
        """Remove and return highest priority item. Blocks if queue is empty."""
        with self._condition:
            start_time = time.time()
            
            while True:
                # Check queues in priority order
                for priority in QueuePriority:
                    queue = self._queues[priority]
                    if queue:
                        item = queue.popleft()
                        item.status = QueueItemStatus.PROCESSING
                        
                        # Move to processing tracking
                        self._processing[item.id] = item
                        self._stats["current_size"] -= 1
                        self._stats["processing_count"] += 1
                        
                        logger.info(
                            "Item dequeued",
                            queue_name=self.name,
                            item_id=item.id,
                            priority=priority.name
                        )
                        
                        return item
                
                # No items available
                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        return None
                    remaining = timeout - elapsed
                    self._condition.wait(remaining)
                else:
                    self._condition.wait()
    
    def mark_completed(self, item_id: str, result: Any = None):
        """Mark item as completed successfully."""
        with self._lock:
            if item_id in self._processing:
                item = self._processing.pop(item_id)
                item.status = QueueItemStatus.COMPLETED
                item.metadata["result"] = result
                item.metadata["completed_at"] = datetime.now()
                
                self._completed.append(item)
                self._stats["total_processed"] += 1
                self._stats["processing_count"] -= 1
                
                logger.info(
                    "Item completed",
                    queue_name=self.name,
                    item_id=item_id,
                    processing_time=(
                        item.metadata["completed_at"] - item.created_at
                    ).total_seconds()
                )
    
    def mark_failed(self, item_id: str, error: str, retry: bool = True):
        """Mark item as failed and optionally retry."""
        with self._lock:
            if item_id not in self._processing:
                return
            
            item = self._processing[item_id]
            item.last_error = error
            item.retry_count += 1
            
            # Check if we should retry
            if retry and item.retry_count <= self.max_retries:
                item.status = QueueItemStatus.RETRYING
                
                # Calculate retry delay
                delay = self.retry_delay_base * (2 ** (item.retry_count - 1))
                retry_time = datetime.now() + timedelta(seconds=delay)
                item.metadata["retry_at"] = retry_time
                
                # Move back to processing (will be handled by retry worker)
                logger.info(
                    "Item scheduled for retry",
                    queue_name=self.name,
                    item_id=item_id,
                    retry_count=item.retry_count,
                    retry_delay=delay
                )
            else:
                # Max retries exceeded or retry disabled
                item.status = QueueItemStatus.FAILED
                self._processing.pop(item_id)
                self._failed.append(item)
                self._stats["total_failed"] += 1
                self._stats["processing_count"] -= 1
                
                logger.error(
                    "Item failed permanently",
                    queue_name=self.name,
                    item_id=item_id,
                    retry_count=item.retry_count,
                    error=error
                )
    
    def requeue_retries(self):
        """Check and requeue items that are ready for retry."""
        now = datetime.now()
        retry_items = []
        
        with self._lock:
            # Find items ready for retry
            for item_id, item in list(self._processing.items()):
                if (item.status == QueueItemStatus.RETRYING and
                    item.metadata.get("retry_at", now) <= now):
                    
                    item.status = QueueItemStatus.PENDING
                    retry_items.append(item)
                    self._processing.pop(item_id)
                    self._stats["processing_count"] -= 1
            
            # Re-enqueue retry items
            for item in retry_items:
                self._queues[item.priority].append(item)
                self._stats["current_size"] += 1
                
                logger.info(
                    "Item requeued for retry",
                    queue_name=self.name,
                    item_id=item.id,
                    retry_count=item.retry_count
                )
            
            if retry_items:
                self._condition.notify_all()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        with self._lock:
            queue_sizes = {
                priority.name: len(queue) 
                for priority, queue in self._queues.items()
            }
            
            return {
                "name": self.name,
                "queue_sizes_by_priority": queue_sizes,
                "processing_count": self._stats["processing_count"],
                "completed_count": len(self._completed),
                "failed_count": len(self._failed),
                "total_enqueued": self._stats["total_enqueued"],
                "total_processed": self._stats["total_processed"],
                "total_failed": self._stats["total_failed"],
                "current_size": self._stats["current_size"],
                "max_size": self.max_size
            }
    
    def get_pending_items(self, priority: Optional[QueuePriority] = None) -> List[QueueItem]:
        """Get list of pending items, optionally filtered by priority."""
        with self._lock:
            items = []
            priorities = [priority] if priority else QueuePriority
            
            for p in priorities:
                items.extend(list(self._queues[p]))
            
            return items
    
    def get_processing_items(self) -> List[QueueItem]:
        """Get list of items currently being processed."""
        with self._lock:
            return list(self._processing.values())
    
    def clear(self):
        """Clear all items from queue (for testing/emergency)."""
        with self._lock:
            for queue in self._queues.values():
                queue.clear()
            
            self._processing.clear()
            self._completed.clear()
            self._failed.clear()
            
            self._stats = {
                "total_enqueued": 0,
                "total_processed": 0,
                "total_failed": 0,
                "current_size": 0,
                "processing_count": 0
            }
            
            logger.warning("Queue cleared", queue_name=self.name)


class QueueManager:
    """Manager for all queue types with monitoring and health checks."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # Initialize queues
        queue_config = config.get("queues", {})
        default_max_size = queue_config.get("max_size", 10000)
        default_max_retries = queue_config.get("max_retries", 3)
        
        self.pending_queue = ThreadSafeQueue(
            "pending_errors",
            max_size=queue_config.get("pending_max_size", default_max_size),
            max_retries=default_max_retries
        )
        
        self.error_queue = ThreadSafeQueue(
            "error_processing",
            max_size=queue_config.get("error_max_size", default_max_size),
            max_retries=default_max_retries
        )
        
        self.fix_queue = ThreadSafeQueue(
            "fix_execution",
            max_size=queue_config.get("fix_max_size", default_max_size),
            max_retries=default_max_retries
        )
        
        # Retry processing
        self._start_retry_processor()
    
    def _start_retry_processor(self):
        """Start background thread to handle retries."""
        def retry_worker():
            while True:
                try:
                    time.sleep(10)  # Check every 10 seconds
                    self.pending_queue.requeue_retries()
                    self.error_queue.requeue_retries()
                    self.fix_queue.requeue_retries()
                except Exception as e:
                    logger.error("Error in retry processor", error=str(e))
        
        retry_thread = threading.Thread(target=retry_worker, daemon=True)
        retry_thread.start()
    
    def submit_error(
        self,
        error_message: str,
        context: str,
        client_id: str,
        service_type: str,
        priority: QueuePriority = QueuePriority.MEDIUM,
        log_source: str = ""
    ) -> str:
        """Submit error for processing."""
        item_id = f"error_{client_id}_{int(time.time() * 1000)}"
        
        error_item = ErrorQueueItem(
            id=item_id,
            priority=priority,
            error_message=error_message,
            context=context,
            client_id=client_id,
            service_type=service_type,
            log_source=log_source
        )
        
        if self.pending_queue.enqueue(error_item):
            return item_id
        else:
            raise Exception("Failed to enqueue error - queue full")
    
    def submit_fix(
        self,
        fix_commands: List[str],
        rollback_commands: List[str],
        validation_commands: List[str],
        target_client_id: str,
        priority: QueuePriority = QueuePriority.MEDIUM,
        requires_restart: bool = False,
        estimated_downtime: int = 0,
        risk_level: str = "medium",
        original_error_id: str = ""
    ) -> str:
        """Submit fix for execution."""
        item_id = f"fix_{target_client_id}_{int(time.time() * 1000)}"
        
        fix_item = FixQueueItem(
            id=item_id,
            priority=priority,
            fix_commands=fix_commands,
            rollback_commands=rollback_commands,
            validation_commands=validation_commands,
            target_client_id=target_client_id,
            requires_restart=requires_restart,
            estimated_downtime=estimated_downtime,
            risk_level=risk_level,
            original_error_id=original_error_id
        )
        
        if self.fix_queue.enqueue(fix_item):
            return item_id
        else:
            raise Exception("Failed to enqueue fix - queue full")
    
    def get_next_error(self, timeout: Optional[float] = None) -> Optional[ErrorQueueItem]:
        """Get next error item for processing."""
        return self.pending_queue.dequeue(timeout)
    
    def get_next_fix(self, timeout: Optional[float] = None) -> Optional[FixQueueItem]:
        """Get next fix item for execution."""
        return self.fix_queue.dequeue(timeout)
    
    def mark_error_completed(self, item_id: str, result: Any = None):
        """Mark error processing as completed."""
        self.pending_queue.mark_completed(item_id, result)
    
    def mark_error_failed(self, item_id: str, error: str, retry: bool = True):
        """Mark error processing as failed."""
        self.pending_queue.mark_failed(item_id, error, retry)
    
    def mark_fix_completed(self, item_id: str, result: Any = None):
        """Mark fix execution as completed."""
        self.fix_queue.mark_completed(item_id, result)
    
    def mark_fix_failed(self, item_id: str, error: str, retry: bool = True):
        """Mark fix execution as failed."""
        self.fix_queue.mark_failed(item_id, error, retry)
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of all queues."""
        return {
            "pending_queue": self.pending_queue.get_stats(),
            "error_queue": self.error_queue.get_stats(),
            "fix_queue": self.fix_queue.get_stats(),
            "timestamp": datetime.now().isoformat()
        }
