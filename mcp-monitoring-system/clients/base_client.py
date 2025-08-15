"""
Base MCP Client for service monitoring and error detection.
Provides common functionality for all service-specific clients.
"""

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Pattern, Callable
import structlog
import yaml
import docker
import psutil

logger = structlog.get_logger(__name__)


@dataclass
class ErrorPattern:
    """Represents an error pattern to detect in logs."""
    name: str
    pattern: str
    severity: int  # 1=critical, 2=high, 3=medium, 4=low
    service_type: str
    description: str
    auto_fix: bool = False
    cooldown_seconds: int = 300  # Don't repeat same error within 5 minutes


@dataclass
class DetectedError:
    """Represents a detected error."""
    pattern_name: str
    message: str
    context: str
    severity: int
    timestamp: datetime
    log_source: str
    service_type: str
    count: int = 1
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)


class LogMonitor:
    """Monitors log files for error patterns."""
    
    def __init__(self, client_id: str, log_paths: List[str], error_patterns: List[ErrorPattern]):
        self.client_id = client_id
        self.log_paths = log_paths
        self.error_patterns = error_patterns
        self.compiled_patterns = {}
        self.file_positions = {}
        self.running = False
        self.monitor_thread = None
        self.error_callback: Optional[Callable] = None
        
        # Compile regex patterns
        for pattern in error_patterns:
            try:
                self.compiled_patterns[pattern.name] = re.compile(
                    pattern.pattern, 
                    re.IGNORECASE | re.MULTILINE
                )
            except re.error as e:
                logger.error(
                    "Invalid regex pattern",
                    client_id=client_id,
                    pattern_name=pattern.name,
                    error=str(e)
                )
        
        # Error deduplication
        self.recent_errors = {}  # pattern_name -> last_reported_time
        
        logger.info(
            "Log monitor initialized",
            client_id=client_id,
            log_paths=log_paths,
            patterns_count=len(self.compiled_patterns)
        )
    
    def set_error_callback(self, callback: Callable[[DetectedError], None]):
        """Set callback function for when errors are detected."""
        self.error_callback = callback
    
    def start(self):
        """Start monitoring log files."""
        if self.running:
            return
        
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_worker, daemon=True)
        self.monitor_thread.start()
        
        logger.info("Log monitoring started", client_id=self.client_id)
    
    def stop(self):
        """Stop monitoring log files."""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        
        logger.info("Log monitoring stopped", client_id=self.client_id)
    
    def _monitor_worker(self):
        """Worker thread for monitoring logs."""
        while self.running:
            try:
                for log_path in self.log_paths:
                    if os.path.exists(log_path):
                        self._check_log_file(log_path)
                
                time.sleep(1)  # Check every second
                
            except Exception as e:
                logger.error(
                    "Error in log monitor worker",
                    client_id=self.client_id,
                    error=str(e)
                )
                time.sleep(5)  # Wait before retrying
    
    def _check_log_file(self, log_path: str):
        """Check a single log file for new errors."""
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                # Seek to last known position
                if log_path in self.file_positions:
                    f.seek(self.file_positions[log_path])
                else:
                    # Start from end of file for new monitoring
                    f.seek(0, 2)
                    self.file_positions[log_path] = f.tell()
                    return
                
                # Read new lines
                lines = f.readlines()
                if lines:
                    self._process_log_lines(lines, log_path)
                    self.file_positions[log_path] = f.tell()
                    
        except Exception as e:
            logger.error(
                "Error reading log file",
                client_id=self.client_id,
                log_path=log_path,
                error=str(e)
            )
    
    def _process_log_lines(self, lines: List[str], log_source: str):
        """Process new log lines for error patterns."""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Check each pattern
            for pattern in self.error_patterns:
                compiled_pattern = self.compiled_patterns.get(pattern.name)
                if not compiled_pattern:
                    continue
                
                match = compiled_pattern.search(line)
                if match:
                    # Check cooldown
                    now = datetime.now()
                    last_reported = self.recent_errors.get(pattern.name)
                    if (last_reported and 
                        (now - last_reported).total_seconds() < pattern.cooldown_seconds):
                        continue
                    
                    # Create error object
                    error = DetectedError(
                        pattern_name=pattern.name,
                        message=line,
                        context=self._get_context_lines(lines, line),
                        severity=pattern.severity,
                        timestamp=now,
                        log_source=log_source,
                        service_type=pattern.service_type
                    )
                    
                    # Update cooldown
                    self.recent_errors[pattern.name] = now
                    
                    # Call error callback
                    if self.error_callback:
                        self.error_callback(error)
                    
                    logger.warning(
                        "Error pattern detected",
                        client_id=self.client_id,
                        pattern=pattern.name,
                        severity=pattern.severity,
                        log_source=log_source
                    )
    
    def _get_context_lines(self, all_lines: List[str], error_line: str, context_size: int = 3) -> str:
        """Get context lines around the error."""
        try:
            error_index = all_lines.index(error_line + '\n')
            start = max(0, error_index - context_size)
            end = min(len(all_lines), error_index + context_size + 1)
            
            context_lines = all_lines[start:end]
            return ''.join(context_lines)
        except ValueError:
            return error_line


class ServiceHealthChecker:
    """Monitors service health and performance metrics."""
    
    def __init__(self, client_id: str, service_name: str, check_interval: int = 60):
        self.client_id = client_id
        self.service_name = service_name
        self.check_interval = check_interval
        self.running = False
        self.check_thread = None
        self.health_callback: Optional[Callable] = None
        
        # Health metrics
        self.last_check_time = None
        self.consecutive_failures = 0
        self.health_history = deque(maxlen=100)
        
    def set_health_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback for health check results."""
        self.health_callback = callback
    
    def start(self):
        """Start health monitoring."""
        if self.running:
            return
        
        self.running = True
        self.check_thread = threading.Thread(target=self._health_check_worker, daemon=True)
        self.check_thread.start()
        
        logger.info("Health monitoring started", client_id=self.client_id, service=self.service_name)
    
    def stop(self):
        """Stop health monitoring."""
        self.running = False
        if self.check_thread:
            self.check_thread.join(timeout=5)
        
        logger.info("Health monitoring stopped", client_id=self.client_id)
    
    def _health_check_worker(self):
        """Worker thread for health checks."""
        while self.running:
            try:
                health_status = self._perform_health_check()
                
                if self.health_callback:
                    self.health_callback(health_status)
                
                # Update failure tracking
                if health_status.get("healthy", False):
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                
                # Store in history
                self.health_history.append({
                    "timestamp": datetime.now(),
                    "status": health_status
                })
                
                self.last_check_time = datetime.now()
                
                time.sleep(self.check_interval)
                
            except Exception as e:
                logger.error(
                    "Error in health check worker",
                    client_id=self.client_id,
                    error=str(e)
                )
                time.sleep(self.check_interval)
    
    def _perform_health_check(self) -> Dict[str, Any]:
        """Perform health check - to be overridden by subclasses."""
        try:
            # Generic system health check
            cpu_usage = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            return {
                "healthy": True,
                "cpu_usage_percent": cpu_usage,
                "memory_usage_percent": memory.percent,
                "disk_usage_percent": disk.percent,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }


class ClientMCPServer(ABC):
    """Base class for all MCP client servers."""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config()
        self.client_id = self.config["client_id"]
        self.service_type = self.config["service_type"]
        self.running = False
        
        # Components
        self.log_monitor: Optional[LogMonitor] = None
        self.health_checker: Optional[ServiceHealthChecker] = None
        self.docker_client: Optional[docker.DockerClient] = None
        
        # Error patterns
        self.error_patterns = self._load_error_patterns()
        
        # Statistics
        self.stats = {
            "start_time": None,
            "errors_detected": 0,
            "health_checks": 0,
            "fixes_applied": 0
        }
        
        # Setup logging
        self._setup_logging()
        
        logger.info(
            "MCP Client initialized",
            client_id=self.client_id,
            service_type=self.service_type
        )
    
    def _load_config(self) -> Dict[str, Any]:
        """Load client configuration."""
        try:
            with open(self.config_path, 'r') as f:
                return yaml.safe_load(f)
        except Exception as e:
            raise Exception(f"Failed to load config: {str(e)}")
    
    def _load_error_patterns(self) -> List[ErrorPattern]:
        """Load error patterns for this service type."""
        patterns = []
        patterns_file = self.config.get("error_patterns_file")
        
        if patterns_file and os.path.exists(patterns_file):
            try:
                with open(patterns_file, 'r') as f:
                    pattern_data = yaml.safe_load(f)
                
                for p in pattern_data.get("patterns", []):
                    if p.get("service_type") == self.service_type:
                        patterns.append(ErrorPattern(
                            name=p["name"],
                            pattern=p["pattern"],
                            severity=p.get("severity", 3),
                            service_type=p["service_type"],
                            description=p.get("description", ""),
                            auto_fix=p.get("auto_fix", False),
                            cooldown_seconds=p.get("cooldown_seconds", 300)
                        ))
                
                logger.info(
                    "Loaded error patterns",
                    client_id=self.client_id,
                    patterns_count=len(patterns)
                )
                
            except Exception as e:
                logger.error(
                    "Failed to load error patterns",
                    client_id=self.client_id,
                    error=str(e)
                )
        
        return patterns
    
    def _setup_logging(self):
        """Setup client-specific logging."""
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer()
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
            context_class=dict,
            cache_logger_on_first_use=True,
        )
    
    @abstractmethod
    def get_log_paths(self) -> List[str]:
        """Get list of log file paths to monitor."""
        pass
    
    @abstractmethod
    def get_service_name(self) -> str:
        """Get the service name for health monitoring."""
        pass
    
    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate service-specific configuration."""
        pass
    
    @abstractmethod
    def apply_fix(self, fix_commands: List[str]) -> Dict[str, Any]:
        """Apply fix commands to the service."""
        pass
    
    def initialize_docker_client(self):
        """Initialize Docker client if service runs in Docker."""
        if self.config.get("docker_enabled", False):
            try:
                self.docker_client = docker.from_env()
                logger.info("Docker client initialized", client_id=self.client_id)
            except Exception as e:
                logger.error(
                    "Failed to initialize Docker client",
                    client_id=self.client_id,
                    error=str(e)
                )
    
    def start_monitoring(self):
        """Start log monitoring and health checks."""
        if self.running:
            return
        
        try:
            # Initialize Docker if needed
            self.initialize_docker_client()
            
            # Start log monitoring
            log_paths = self.get_log_paths()
            if log_paths and self.error_patterns:
                self.log_monitor = LogMonitor(
                    self.client_id,
                    log_paths,
                    self.error_patterns
                )
                self.log_monitor.set_error_callback(self._on_error_detected)
                self.log_monitor.start()
            
            # Start health monitoring
            service_name = self.get_service_name()
            if service_name:
                self.health_checker = ServiceHealthChecker(
                    self.client_id,
                    service_name,
                    self.config.get("health_check_interval", 60)
                )
                self.health_checker.set_health_callback(self._on_health_check)
                self.health_checker.start()
            
            self.running = True
            self.stats["start_time"] = datetime.now()
            
            logger.info("Client monitoring started", client_id=self.client_id)
            
        except Exception as e:
            logger.error(
                "Failed to start monitoring",
                client_id=self.client_id,
                error=str(e)
            )
            raise
    
    def stop_monitoring(self):
        """Stop all monitoring activities."""
        if not self.running:
            return
        
        self.running = False
        
        if self.log_monitor:
            self.log_monitor.stop()
        
        if self.health_checker:
            self.health_checker.stop()
        
        logger.info("Client monitoring stopped", client_id=self.client_id)
    
    def _on_error_detected(self, error: DetectedError):
        """Handle detected errors."""
        self.stats["errors_detected"] += 1
        
        # Send error to main MCP server
        self._send_error_to_server(error)
        
        logger.error(
            "Error detected and reported",
            client_id=self.client_id,
            pattern=error.pattern_name,
            severity=error.severity
        )
    
    def _on_health_check(self, health_status: Dict[str, Any]):
        """Handle health check results."""
        self.stats["health_checks"] += 1
        
        # Log health issues
        if not health_status.get("healthy", True):
            logger.warning(
                "Health check failed",
                client_id=self.client_id,
                status=health_status
            )
            
            # Create error for health check failure
            error = DetectedError(
                pattern_name="health_check_failed",
                message=f"Health check failed: {health_status.get('error', 'Unknown error')}",
                context=json.dumps(health_status, indent=2),
                severity=2,  # High priority
                timestamp=datetime.now(),
                log_source="health_checker",
                service_type=self.service_type
            )
            
            self._send_error_to_server(error)
    
    def _send_error_to_server(self, error: DetectedError):
        """Send detected error to main MCP server."""
        # This would be implemented to communicate with the main server
        # For now, just log the error
        logger.info(
            "Would send error to main server",
            client_id=self.client_id,
            error_pattern=error.pattern_name,
            message=error.message[:200],
            severity=error.severity
        )
    
    def get_status(self) -> Dict[str, Any]:
        """Get current client status."""
        status = {
            "client_id": self.client_id,
            "service_type": self.service_type,
            "running": self.running,
            "statistics": self.stats
        }
        
        if self.log_monitor:
            status["log_monitoring"] = {
                "active": self.log_monitor.running,
                "patterns_count": len(self.error_patterns),
                "recent_errors_count": len(self.log_monitor.recent_errors)
            }
        
        if self.health_checker:
            status["health_monitoring"] = {
                "active": self.health_checker.running,
                "last_check": self.health_checker.last_check_time.isoformat() if self.health_checker.last_check_time else None,
                "consecutive_failures": self.health_checker.consecutive_failures,
                "history_size": len(self.health_checker.health_history)
            }
        
        return status
    
    def reload_config(self):
        """Reload configuration and restart monitoring if needed."""
        try:
            old_config = self.config
            self.config = self._load_config()
            
            # Restart monitoring if configuration changed significantly
            if (old_config.get("log_paths") != self.config.get("log_paths") or
                old_config.get("error_patterns_file") != self.config.get("error_patterns_file")):
                
                logger.info("Configuration changed, restarting monitoring", client_id=self.client_id)
                self.stop_monitoring()
                self.error_patterns = self._load_error_patterns()
                self.start_monitoring()
            
        except Exception as e:
            logger.error(
                "Failed to reload configuration",
                client_id=self.client_id,
                error=str(e)
            )
    
    def run(self):
        """Main run loop for the client."""
        try:
            self.start_monitoring()
            
            # Keep running until interrupted
            while self.running:
                time.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Client interrupted by user", client_id=self.client_id)
        except Exception as e:
            logger.error("Client error", client_id=self.client_id, error=str(e))
        finally:
            self.stop_monitoring()
