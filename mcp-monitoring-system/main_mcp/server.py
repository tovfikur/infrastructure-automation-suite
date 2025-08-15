"""
Main MCP Server for Distributed Monitoring System.
Orchestrates error processing, Claude API communication, and fix execution.
"""

import asyncio
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Set
from dataclasses import asdict
import structlog
import yaml
from pathlib import Path

from .token_manager import TokenLimitManager, TokenUsage
from .claude_client import ClaudeClient, FixResponse, BatchFixResponse
from .queues import QueueManager, QueuePriority, ErrorQueueItem, FixQueueItem
from .ssh_manager import SSHConnectionManager, SSHConnectionInfo

logger = structlog.get_logger(__name__)


class MCPServerError(Exception):
    """Custom exception for MCP server errors."""
    pass


class MainMCPServer:
    """Main MCP server orchestrating the distributed monitoring system."""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config()
        self.running = False
        self.shutdown_event = threading.Event()
        
        # Core components
        self.token_manager: Optional[TokenLimitManager] = None
        self.claude_client: Optional[ClaudeClient] = None
        self.queue_manager: Optional[QueueManager] = None
        self.ssh_manager: Optional[SSHConnectionManager] = None
        
        # Client registry
        self.registered_clients: Dict[str, Dict[str, Any]] = {}
        self.client_health_status: Dict[str, Dict[str, Any]] = {}
        
        # Processing workers
        self.worker_threads: List[threading.Thread] = []
        self.error_processors: int = self.config.get("processing", {}).get("error_processors", 3)
        self.fix_executors: int = self.config.get("processing", {}).get("fix_executors", 2)
        
        # Statistics and monitoring
        self.stats = {
            "start_time": datetime.now(),
            "errors_processed": 0,
            "fixes_applied": 0,
            "fixes_failed": 0,
            "clients_connected": 0,
            "total_uptime": 0
        }
        
        # Setup structured logging
        self._setup_logging()
        
        logger.info("MCP Server initialized", config_path=config_path)
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        try:
            with open(self.config_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # Validate required configuration (claude_desktop is optional now)
            required_keys = ['token_limits', 'ssh_settings']
            for key in required_keys:
                if key not in config:
                    raise MCPServerError(f"Missing required configuration: {key}")
            
            return config
            
        except FileNotFoundError:
            raise MCPServerError(f"Configuration file not found: {self.config_path}")
        except yaml.YAMLError as e:
            raise MCPServerError(f"Invalid YAML configuration: {str(e)}")
        except Exception as e:
            raise MCPServerError(f"Error loading configuration: {str(e)}")
    
    def _setup_logging(self):
        """Setup structured logging configuration."""
        log_config = self.config.get("logging", {})
        
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer()
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    
    async def initialize(self):
        """Initialize all components."""
        try:
            logger.info("Initializing MCP Server components")
            
            # Initialize token manager
            token_config = self.config.get("token_limits", {})
            self.token_manager = TokenLimitManager(token_config)
            
            # Initialize Claude client
            claude_config = self.config.get("claude_desktop", {})
            self.claude_client = ClaudeClient(
                claude_desktop_path=claude_config.get("path"),
                model=claude_config.get("model", "claude-3-5-sonnet-20241022"),
                max_retries=claude_config.get("max_retries", 3),
                timeout=claude_config.get("timeout", 300)
            )
            
            # Initialize queue manager
            queue_config = self.config.get("queues", {})
            self.queue_manager = QueueManager(queue_config)
            
            # Initialize SSH manager
            ssh_config = self.config.get("ssh_settings", {})
            self.ssh_manager = SSHConnectionManager(
                max_connections_per_client=ssh_config.get("max_connections_per_client", 5),
                connection_idle_timeout=ssh_config.get("connection_idle_timeout", 300),
                health_check_interval=ssh_config.get("health_check_interval", 60)
            )
            
            logger.info("All components initialized successfully")
            
        except Exception as e:
            logger.error("Failed to initialize components", error=str(e))
            raise MCPServerError(f"Initialization failed: {str(e)}")
    
    def register_client(
        self,
        client_id: str,
        host: str,
        port: int,
        username: str,
        private_key_path: str,
        service_type: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Register a new MCP client for monitoring."""
        try:
            # Create SSH connection info
            ssh_info = SSHConnectionInfo(
                host=host,
                port=port,
                username=username,
                private_key_path=private_key_path,
                client_id=client_id,
                connection_timeout=self.config.get("ssh_settings", {}).get("connection_timeout", 30),
                command_timeout=self.config.get("ssh_settings", {}).get("command_timeout", 300)
            )
            
            # Register with SSH manager
            self.ssh_manager.register_client(ssh_info)
            
            # Store client information
            self.registered_clients[client_id] = {
                "host": host,
                "port": port,
                "username": username,
                "service_type": service_type,
                "registered_at": datetime.now(),
                "metadata": metadata or {}
            }
            
            # Test connection
            if self.ssh_manager.test_connection(client_id):
                self.client_health_status[client_id] = {
                    "status": "healthy",
                    "last_check": datetime.now(),
                    "connection_errors": 0
                }
                self.stats["clients_connected"] += 1
                
                logger.info(
                    "Client registered successfully",
                    client_id=client_id,
                    host=host,
                    service_type=service_type
                )
                return True
            else:
                logger.error(
                    "Client registration failed - connection test failed",
                    client_id=client_id,
                    host=host
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error registering client",
                client_id=client_id,
                error=str(e)
            )
            return False
    
    def handle_error(
        self,
        client_id: str,
        error_message: str,
        context: str,
        priority: int = 3,
        log_source: str = ""
    ) -> str:
        """Handle incoming error from client."""
        if client_id not in self.registered_clients:
            raise MCPServerError(f"Unknown client: {client_id}")
        
        client_info = self.registered_clients[client_id]
        service_type = client_info["service_type"]
        
        # Map priority to enum
        priority_map = {1: QueuePriority.CRITICAL, 2: QueuePriority.HIGH, 
                       3: QueuePriority.MEDIUM, 4: QueuePriority.LOW}
        queue_priority = priority_map.get(priority, QueuePriority.MEDIUM)
        
        # Submit to queue
        try:
            error_id = self.queue_manager.submit_error(
                error_message=error_message,
                context=context,
                client_id=client_id,
                service_type=service_type,
                priority=queue_priority,
                log_source=log_source
            )
            
            logger.info(
                "Error submitted for processing",
                client_id=client_id,
                error_id=error_id,
                priority=priority
            )
            
            return error_id
            
        except Exception as e:
            logger.error(
                "Failed to submit error",
                client_id=client_id,
                error=str(e)
            )
            raise MCPServerError(f"Failed to submit error: {str(e)}")
    
    async def _error_processor_worker(self, worker_id: int):
        """Worker thread for processing errors."""
        logger.info("Error processor worker started", worker_id=worker_id)
        
        while not self.shutdown_event.is_set():
            try:
                # Get next error from queue
                error_item = self.queue_manager.get_next_error(timeout=1.0)
                if not error_item:
                    continue
                
                logger.info(
                    "Processing error",
                    worker_id=worker_id,
                    error_id=error_item.id,
                    client_id=error_item.client_id
                )
                
                # Check token limits and submit to Claude
                estimated_tokens = len(error_item.error_message) // 4 + len(error_item.context) // 4 + 1000
                
                # Try to get fix from Claude
                fix_response = await self.claude_client.get_fix_for_error(
                    error_message=error_item.error_message,
                    context=error_item.context,
                    service_type=error_item.service_type,
                    request_id=error_item.id
                )
                
                if fix_response.success and fix_response.confidence > 0.6:
                    # Submit fix for execution
                    fix_priority = error_item.priority
                    if fix_response.risk_level == "high":
                        fix_priority = QueuePriority.LOW  # High risk fixes get low priority
                    
                    fix_id = self.queue_manager.submit_fix(
                        fix_commands=fix_response.fix_commands,
                        rollback_commands=fix_response.rollback_commands,
                        validation_commands=fix_response.validation_commands,
                        target_client_id=error_item.client_id,
                        priority=fix_priority,
                        requires_restart=fix_response.requires_restart,
                        estimated_downtime=fix_response.estimated_downtime,
                        risk_level=fix_response.risk_level,
                        original_error_id=error_item.id
                    )
                    
                    logger.info(
                        "Fix submitted for execution",
                        error_id=error_item.id,
                        fix_id=fix_id,
                        confidence=fix_response.confidence,
                        risk_level=fix_response.risk_level
                    )
                    
                    # Mark error as completed
                    self.queue_manager.mark_error_completed(error_item.id, {
                        "fix_id": fix_id,
                        "confidence": fix_response.confidence,
                        "risk_level": fix_response.risk_level
                    })
                    
                    self.stats["errors_processed"] += 1
                    
                else:
                    # Fix not confident enough or failed
                    logger.warning(
                        "Fix rejected due to low confidence or failure",
                        error_id=error_item.id,
                        confidence=fix_response.confidence if fix_response else 0,
                        success=fix_response.success if fix_response else False
                    )
                    
                    self.queue_manager.mark_error_failed(
                        error_item.id,
                        f"Low confidence fix: {fix_response.confidence if fix_response else 0}",
                        retry=True
                    )
                
            except Exception as e:
                logger.error(
                    "Error in processor worker",
                    worker_id=worker_id,
                    error=str(e)
                )
                if 'error_item' in locals():
                    self.queue_manager.mark_error_failed(error_item.id, str(e))
                
                # Avoid tight loop on persistent errors
                await asyncio.sleep(1)
    
    async def _fix_executor_worker(self, worker_id: int):
        """Worker thread for executing fixes."""
        logger.info("Fix executor worker started", worker_id=worker_id)
        
        while not self.shutdown_event.is_set():
            try:
                # Get next fix from queue
                fix_item = self.queue_manager.get_next_fix(timeout=1.0)
                if not fix_item:
                    continue
                
                logger.info(
                    "Executing fix",
                    worker_id=worker_id,
                    fix_id=fix_item.id,
                    client_id=fix_item.target_client_id,
                    risk_level=fix_item.risk_level
                )
                
                # Execute validation commands first
                validation_success = True
                if fix_item.validation_commands:
                    for cmd in fix_item.validation_commands:
                        result = await self.ssh_manager.execute_command_async(
                            fix_item.target_client_id,
                            cmd,
                            timeout=60
                        )
                        if not result.success:
                            validation_success = False
                            logger.warning(
                                "Validation command failed",
                                fix_id=fix_item.id,
                                command=cmd,
                                error=result.stderr
                            )
                            break
                
                if not validation_success:
                    self.queue_manager.mark_fix_failed(
                        fix_item.id,
                        "Pre-execution validation failed"
                    )
                    continue
                
                # Execute fix commands
                fix_success = True
                executed_commands = []
                
                for cmd in fix_item.fix_commands:
                    result = await self.ssh_manager.execute_command_async(
                        fix_item.target_client_id,
                        cmd,
                        timeout=300
                    )
                    
                    executed_commands.append({
                        "command": cmd,
                        "success": result.success,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code
                    })
                    
                    if not result.success:
                        fix_success = False
                        logger.error(
                            "Fix command failed",
                            fix_id=fix_item.id,
                            command=cmd,
                            error=result.stderr
                        )
                        break
                
                if fix_success:
                    # Run post-execution validation
                    if fix_item.validation_commands:
                        for cmd in fix_item.validation_commands:
                            result = await self.ssh_manager.execute_command_async(
                                fix_item.target_client_id,
                                cmd,
                                timeout=60
                            )
                            if not result.success:
                                fix_success = False
                                logger.warning(
                                    "Post-execution validation failed",
                                    fix_id=fix_item.id,
                                    command=cmd
                                )
                                break
                
                if fix_success:
                    self.queue_manager.mark_fix_completed(fix_item.id, {
                        "executed_commands": executed_commands,
                        "requires_restart": fix_item.requires_restart,
                        "estimated_downtime": fix_item.estimated_downtime
                    })
                    self.stats["fixes_applied"] += 1
                    
                    logger.info(
                        "Fix executed successfully",
                        fix_id=fix_item.id,
                        client_id=fix_item.target_client_id
                    )
                    
                else:
                    # Execute rollback commands
                    if fix_item.rollback_commands:
                        logger.info("Executing rollback commands", fix_id=fix_item.id)
                        for cmd in fix_item.rollback_commands:
                            await self.ssh_manager.execute_command_async(
                                fix_item.target_client_id,
                                cmd,
                                timeout=300
                            )
                    
                    self.queue_manager.mark_fix_failed(
                        fix_item.id,
                        "Fix execution failed, rollback completed"
                    )
                    self.stats["fixes_failed"] += 1
                
            except Exception as e:
                logger.error(
                    "Error in fix executor worker",
                    worker_id=worker_id,
                    error=str(e)
                )
                if 'fix_item' in locals():
                    self.queue_manager.mark_fix_failed(fix_item.id, str(e))
                
                # Avoid tight loop on persistent errors
                await asyncio.sleep(1)
    
    async def start_workers(self):
        """Start all worker threads."""
        logger.info("Starting worker threads")
        
        # Start error processor workers
        for i in range(self.error_processors):
            task = asyncio.create_task(self._error_processor_worker(i))
            # Store reference to prevent garbage collection
            self.worker_threads.append(task)
        
        # Start fix executor workers  
        for i in range(self.fix_executors):
            task = asyncio.create_task(self._fix_executor_worker(i))
            self.worker_threads.append(task)
        
        logger.info(
            "All workers started",
            error_processors=self.error_processors,
            fix_executors=self.fix_executors
        )
    
    async def start(self):
        """Start the MCP server."""
        if self.running:
            return
        
        try:
            logger.info("Starting MCP Server")
            
            # Initialize components
            await self.initialize()
            
            # Start worker threads
            await self.start_workers()
            
            self.running = True
            self.stats["start_time"] = datetime.now()
            
            logger.info("MCP Server started successfully")
            
            # Run until shutdown
            while not self.shutdown_event.is_set():
                await asyncio.sleep(1)
                self.stats["total_uptime"] = (datetime.now() - self.stats["start_time"]).total_seconds()
            
        except Exception as e:
            logger.error("Error starting MCP Server", error=str(e))
            await self.shutdown()
            raise
    
    async def shutdown(self):
        """Graceful shutdown of the MCP server."""
        if not self.running:
            return
        
        logger.info("Shutting down MCP Server")
        
        # Signal shutdown
        self.shutdown_event.set()
        self.running = False
        
        # Cancel all worker tasks
        for task in self.worker_threads:
            if hasattr(task, 'cancel'):
                task.cancel()
        
        # Wait for tasks to complete
        if self.worker_threads:
            await asyncio.gather(*self.worker_threads, return_exceptions=True)
        
        # Cleanup components
        if self.ssh_manager:
            self.ssh_manager.close_all_connections()
        
        logger.info("MCP Server shutdown complete")
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive server status."""
        status = {
            "server": {
                "running": self.running,
                "uptime_seconds": self.stats["total_uptime"],
                "start_time": self.stats["start_time"].isoformat()
            },
            "statistics": self.stats,
            "clients": {
                "total_registered": len(self.registered_clients),
                "health_status": self.client_health_status
            }
        }
        
        if self.queue_manager:
            status["queues"] = self.queue_manager.get_health_status()
        
        if self.token_manager:
            status["token_manager"] = self.token_manager.get_status()
        
        if self.ssh_manager:
            status["ssh_manager"] = self.ssh_manager.get_overall_stats()
        
        return status


async def main():
    """Main entry point for the MCP server."""
    if len(sys.argv) < 2:
        print("Usage: python server.py <config_path>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    server = MainMCPServer(config_path)
    
    # Setup signal handlers
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down")
        asyncio.create_task(server.shutdown())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error("Server error", error=str(e))
    finally:
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
