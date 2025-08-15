"""
Token Limit Manager for MCP Distributed Monitoring System.
Handles rate limiting, token counting, batching, and fallback strategies.
"""

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class TokenUsage:
    """Track token usage for requests."""
    input_tokens: int
    output_tokens: int
    timestamp: datetime
    request_id: str


@dataclass
class BatchRequest:
    """Represents a batched request to Claude API."""
    request_id: str
    error_messages: List[str]
    context: str
    priority: int
    timestamp: datetime
    client_id: str


class RateLimiter:
    """Token-aware rate limiter with exponential backoff."""
    
    def __init__(
        self,
        max_tokens_per_minute: int = 40000,
        max_requests_per_minute: int = 50,
        cooldown_seconds: int = 60,
        max_backoff: int = 300
    ):
        self.max_tokens_per_minute = max_tokens_per_minute
        self.max_requests_per_minute = max_requests_per_minute
        self.cooldown_seconds = cooldown_seconds
        self.max_backoff = max_backoff
        
        # Sliding window for rate tracking
        self.token_usage = deque(maxlen=1000)
        self.request_times = deque(maxlen=1000)
        
        # Backoff tracking
        self.consecutive_failures = 0
        self.last_failure_time = None
        
        self._lock = asyncio.Lock()

    async def can_make_request(self, estimated_tokens: int) -> Tuple[bool, int]:
        """Check if request can be made. Returns (can_make, wait_seconds)."""
        async with self._lock:
            now = datetime.now()
            
            # Check if we're in cooldown period
            if self.last_failure_time:
                backoff_time = min(2 ** self.consecutive_failures, self.max_backoff)
                if (now - self.last_failure_time).seconds < backoff_time:
                    return False, backoff_time
            
            # Clean old entries (beyond 1 minute)
            cutoff = now - timedelta(minutes=1)
            while self.token_usage and self.token_usage[0].timestamp < cutoff:
                self.token_usage.popleft()
            while self.request_times and self.request_times[0] < cutoff:
                self.request_times.popleft()
            
            # Check current usage
            current_tokens = sum(u.input_tokens + u.output_tokens for u in self.token_usage)
            current_requests = len(self.request_times)
            
            # Check limits
            if (current_tokens + estimated_tokens) > self.max_tokens_per_minute:
                wait_time = 60 - min((now - self.token_usage[0].timestamp).seconds 
                                   if self.token_usage else 0, 60)
                return False, wait_time
            
            if current_requests >= self.max_requests_per_minute:
                wait_time = 60 - min((now - self.request_times[0]).seconds 
                                   if self.request_times else 0, 60)
                return False, wait_time
            
            return True, 0

    async def record_usage(self, usage: TokenUsage, success: bool = True):
        """Record token usage and update rate limiting state."""
        async with self._lock:
            self.token_usage.append(usage)
            self.request_times.append(usage.timestamp)
            
            if success:
                self.consecutive_failures = 0
                self.last_failure_time = None
            else:
                self.consecutive_failures += 1
                self.last_failure_time = datetime.now()
                
            logger.info(
                "Token usage recorded",
                request_id=usage.request_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                success=success,
                consecutive_failures=self.consecutive_failures
            )


class TokenLimitManager:
    """Main token limit manager with intelligent batching and caching."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.rate_limiter = RateLimiter(
            max_tokens_per_minute=config.get("max_tokens_per_minute", 40000),
            max_requests_per_minute=config.get("max_requests_per_minute", 50),
            cooldown_seconds=config.get("cooldown_seconds", 60)
        )
        
        # Caching for similar errors
        self.error_cache = {}  # error_hash -> cached_response
        self.cache_ttl = config.get("cache_ttl_hours", 24) * 3600
        
        # Batching configuration
        self.batch_size = config.get("batch_size", 5)
        self.batch_timeout = config.get("batch_timeout_seconds", 30)
        self.pending_batches = defaultdict(list)
        self.batch_timers = {}
        
        # Priority handling
        self.priority_queues = {
            1: deque(),  # Critical
            2: deque(),  # High  
            3: deque(),  # Medium
            4: deque()   # Low
        }
        
        self._batch_lock = asyncio.Lock()
        
    def _hash_error(self, error_message: str, context: str = "") -> str:
        """Create a hash for error deduplication."""
        import hashlib
        content = f"{error_message.lower().strip()}{context[:500]}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def _get_cached_response(self, error_hash: str) -> Optional[Dict[str, Any]]:
        """Get cached response if available and not expired."""
        if error_hash in self.error_cache:
            cached_data = self.error_cache[error_hash]
            if time.time() - cached_data["timestamp"] < self.cache_ttl:
                logger.info("Using cached response", error_hash=error_hash)
                return cached_data["response"]
            else:
                # Clean expired cache
                del self.error_cache[error_hash]
        return None
    
    def _cache_response(self, error_hash: str, response: Dict[str, Any]):
        """Cache a response for future use."""
        self.error_cache[error_hash] = {
            "response": response,
            "timestamp": time.time()
        }
        
        # Clean old cache entries (keep last 1000)
        if len(self.error_cache) > 1000:
            oldest_keys = sorted(
                self.error_cache.keys(),
                key=lambda k: self.error_cache[k]["timestamp"]
            )[:100]
            for key in oldest_keys:
                del self.error_cache[key]
    
    async def submit_request(
        self,
        error_message: str,
        context: str,
        client_id: str,
        priority: int = 3
    ) -> Optional[str]:
        """Submit a request for processing with intelligent batching and caching."""
        
        # Check cache first
        error_hash = self._hash_error(error_message, context)
        cached_response = self._get_cached_response(error_hash)
        if cached_response:
            return cached_response.get("fix", None)
        
        # Create batch request
        batch_request = BatchRequest(
            request_id=f"{client_id}_{int(time.time() * 1000)}",
            error_messages=[error_message],
            context=context,
            priority=priority,
            timestamp=datetime.now(),
            client_id=client_id
        )
        
        async with self._batch_lock:
            # Add to priority queue
            self.priority_queues[priority].append(batch_request)
            
            # Check if we should create a batch immediately
            if priority == 1:  # Critical - process immediately
                return await self._process_request_immediately(batch_request, error_hash)
            
            # For other priorities, add to batching queue
            await self._maybe_create_batch(priority)
        
        return None  # Will be processed asynchronously
    
    async def _process_request_immediately(
        self,
        request: BatchRequest,
        error_hash: str
    ) -> Optional[str]:
        """Process a critical request immediately."""
        estimated_tokens = min(len(request.context) // 4 + 1000, 4000)
        
        can_make, wait_seconds = await self.rate_limiter.can_make_request(estimated_tokens)
        if not can_make:
            logger.warning(
                "Rate limit exceeded for critical request",
                wait_seconds=wait_seconds,
                request_id=request.request_id
            )
            return None
        
        # This would be handled by the Claude client
        return None
    
    async def _maybe_create_batch(self, priority: int):
        """Check if we should create a batch for the given priority."""
        queue = self.priority_queues[priority]
        
        if len(queue) >= self.batch_size:
            await self._create_batch(priority)
        elif priority not in self.batch_timers:
            # Start timer for this priority level
            self.batch_timers[priority] = asyncio.create_task(
                self._batch_timeout_handler(priority)
            )
    
    async def _create_batch(self, priority: int):
        """Create and process a batch of requests."""
        queue = self.priority_queues[priority]
        if not queue:
            return
        
        # Get batch of requests
        batch = []
        for _ in range(min(self.batch_size, len(queue))):
            if queue:
                batch.append(queue.popleft())
        
        if not batch:
            return
        
        # Cancel timer if exists
        if priority in self.batch_timers:
            self.batch_timers[priority].cancel()
            del self.batch_timers[priority]
        
        # Estimate tokens for entire batch
        estimated_tokens = sum(len(req.context) // 4 + len(req.error_messages[0]) // 4 
                             for req in batch) + 2000
        
        can_make, wait_seconds = await self.rate_limiter.can_make_request(estimated_tokens)
        if not can_make:
            # Re-queue the requests
            for req in reversed(batch):
                self.priority_queues[priority].appendleft(req)
            
            logger.warning(
                "Rate limit exceeded for batch",
                priority=priority,
                wait_seconds=wait_seconds,
                batch_size=len(batch)
            )
            
            # Schedule retry
            await asyncio.sleep(wait_seconds)
            await self._create_batch(priority)
            return
        
        logger.info(
            "Creating batch for processing",
            priority=priority,
            batch_size=len(batch),
            estimated_tokens=estimated_tokens
        )
        
        # This batch would be sent to Claude client for processing
        # For now, just log the batch creation
    
    async def _batch_timeout_handler(self, priority: int):
        """Handle batch timeout - process whatever is in queue."""
        try:
            await asyncio.sleep(self.batch_timeout)
            await self._create_batch(priority)
        except asyncio.CancelledError:
            pass
        finally:
            if priority in self.batch_timers:
                del self.batch_timers[priority]
    
    def get_status(self) -> Dict[str, Any]:
        """Get current status of the token manager."""
        now = datetime.now()
        cutoff = now - timedelta(minutes=1)
        
        # Count recent usage
        recent_tokens = sum(
            u.input_tokens + u.output_tokens 
            for u in self.rate_limiter.token_usage 
            if u.timestamp > cutoff
        )
        
        recent_requests = sum(
            1 for t in self.rate_limiter.request_times 
            if t > cutoff
        )
        
        return {
            "recent_token_usage": recent_tokens,
            "recent_request_count": recent_requests,
            "cache_size": len(self.error_cache),
            "pending_requests": {
                priority: len(queue) 
                for priority, queue in self.priority_queues.items()
            },
            "consecutive_failures": self.rate_limiter.consecutive_failures,
            "max_tokens_per_minute": self.rate_limiter.max_tokens_per_minute,
            "max_requests_per_minute": self.rate_limiter.max_requests_per_minute
        }
