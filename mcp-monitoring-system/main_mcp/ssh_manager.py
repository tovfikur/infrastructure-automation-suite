"""
SSH Connection Manager for MCP Distributed Monitoring System.
Handles secure SSH connections to client servers with connection pooling and retry logic.
"""

import asyncio
import os
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple, Union
from datetime import datetime, timedelta
import structlog
import paramiko
from paramiko import SSHClient, AutoAddPolicy
from concurrent.futures import ThreadPoolExecutor

logger = structlog.get_logger(__name__)


@dataclass
class SSHConnectionInfo:
    """Connection information for SSH clients."""
    host: str
    port: int
    username: str
    private_key_path: str
    client_id: str
    connection_timeout: int = 30
    command_timeout: int = 300
    max_retries: int = 3


@dataclass
class CommandResult:
    """Result of SSH command execution."""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    execution_time: float
    command: str
    timestamp: datetime


class SSHConnection:
    """Wrapper for individual SSH connection with health monitoring."""
    
    def __init__(self, connection_info: SSHConnectionInfo):
        self.info = connection_info
        self.client: Optional[SSHClient] = None
        self.connected = False
        self.last_used = datetime.now()
        self.connection_count = 0
        self.error_count = 0
        self._lock = threading.Lock()
    
    def connect(self) -> bool:
        """Establish SSH connection."""
        try:
            if self.client:
                self.client.close()
            
            self.client = SSHClient()
            self.client.set_missing_host_key_policy(AutoAddPolicy())
            
            # Load private key
            if not os.path.exists(self.info.private_key_path):
                raise FileNotFoundError(f"Private key not found: {self.info.private_key_path}")
            
            private_key = paramiko.RSAKey.from_private_key_file(self.info.private_key_path)
            
            # Connect
            self.client.connect(
                hostname=self.info.host,
                port=self.info.port,
                username=self.info.username,
                pkey=private_key,
                timeout=self.info.connection_timeout,
                auth_timeout=self.info.connection_timeout,
                banner_timeout=self.info.connection_timeout
            )
            
            self.connected = True
            self.connection_count += 1
            self.last_used = datetime.now()
            
            logger.info(
                "SSH connection established",
                client_id=self.info.client_id,
                host=self.info.host,
                port=self.info.port
            )
            
            return True
            
        except Exception as e:
            self.connected = False
            self.error_count += 1
            logger.error(
                "SSH connection failed",
                client_id=self.info.client_id,
                host=self.info.host,
                error=str(e)
            )
            return False
    
    def is_alive(self) -> bool:
        """Check if connection is alive."""
        if not self.connected or not self.client:
            return False
        
        try:
            transport = self.client.get_transport()
            if not transport or not transport.is_active():
                return False
            
            # Send keep-alive
            transport.send_ignore()
            return True
            
        except Exception:
            self.connected = False
            return False
    
    def execute_command(
        self,
        command: str,
        timeout: Optional[int] = None
    ) -> CommandResult:
        """Execute command over SSH connection."""
        timeout = timeout or self.info.command_timeout
        start_time = time.time()
        
        with self._lock:
            if not self.is_alive():
                if not self.connect():
                    return CommandResult(
                        success=False,
                        stdout="",
                        stderr="Connection failed",
                        exit_code=-1,
                        execution_time=time.time() - start_time,
                        command=command,
                        timestamp=datetime.now()
                    )
            
            try:
                stdin, stdout, stderr = self.client.exec_command(
                    command,
                    timeout=timeout,
                    get_pty=False
                )
                
                # Read output
                stdout_data = stdout.read().decode('utf-8', errors='replace')
                stderr_data = stderr.read().decode('utf-8', errors='replace')
                exit_code = stdout.channel.recv_exit_status()
                
                self.last_used = datetime.now()
                execution_time = time.time() - start_time
                
                result = CommandResult(
                    success=(exit_code == 0),
                    stdout=stdout_data,
                    stderr=stderr_data,
                    exit_code=exit_code,
                    execution_time=execution_time,
                    command=command,
                    timestamp=datetime.now()
                )
                
                logger.info(
                    "Command executed",
                    client_id=self.info.client_id,
                    command=command[:100],
                    success=result.success,
                    exit_code=exit_code,
                    execution_time=execution_time
                )
                
                return result
                
            except Exception as e:
                self.error_count += 1
                logger.error(
                    "Command execution failed",
                    client_id=self.info.client_id,
                    command=command[:100],
                    error=str(e)
                )
                
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=str(e),
                    exit_code=-1,
                    execution_time=time.time() - start_time,
                    command=command,
                    timestamp=datetime.now()
                )
    
    def close(self):
        """Close SSH connection."""
        with self._lock:
            if self.client:
                try:
                    self.client.close()
                except:
                    pass
                finally:
                    self.client = None
                    self.connected = False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        return {
            "client_id": self.info.client_id,
            "host": self.info.host,
            "port": self.info.port,
            "connected": self.connected,
            "last_used": self.last_used.isoformat(),
            "connection_count": self.connection_count,
            "error_count": self.error_count,
            "uptime_seconds": (datetime.now() - self.last_used).total_seconds()
        }


class SSHConnectionManager:
    """Manages pool of SSH connections with health monitoring and failover."""
    
    def __init__(
        self,
        max_connections_per_client: int = 5,
        connection_idle_timeout: int = 300,  # 5 minutes
        health_check_interval: int = 60,     # 1 minute
        max_command_retries: int = 3
    ):
        self.max_connections_per_client = max_connections_per_client
        self.connection_idle_timeout = connection_idle_timeout
        self.health_check_interval = health_check_interval
        self.max_command_retries = max_command_retries
        
        # Connection pools per client
        self._connection_pools: Dict[str, List[SSHConnection]] = {}
        self._client_configs: Dict[str, SSHConnectionInfo] = {}
        
        # Thread safety
        self._pool_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="ssh-")
        
        # Health monitoring
        self._health_check_task = None
        self._start_health_monitor()
        
        # Statistics
        self._stats = {
            "total_connections": 0,
            "active_connections": 0,
            "total_commands": 0,
            "successful_commands": 0,
            "failed_commands": 0
        }
    
    def register_client(self, connection_info: SSHConnectionInfo):
        """Register a new SSH client for connection management."""
        with self._pool_lock:
            self._client_configs[connection_info.client_id] = connection_info
            self._connection_pools[connection_info.client_id] = []
            
            logger.info(
                "SSH client registered",
                client_id=connection_info.client_id,
                host=connection_info.host,
                port=connection_info.port
            )
    
    def _get_connection(self, client_id: str) -> Optional[SSHConnection]:
        """Get available connection from pool or create new one."""
        with self._pool_lock:
            if client_id not in self._connection_pools:
                logger.error("Client not registered", client_id=client_id)
                return None
            
            pool = self._connection_pools[client_id]
            config = self._client_configs[client_id]
            
            # Try to find available connection
            for conn in pool:
                if conn.is_alive():
                    return conn
            
            # Remove dead connections
            pool[:] = [conn for conn in pool if conn.is_alive()]
            
            # Create new connection if pool has space
            if len(pool) < self.max_connections_per_client:
                new_conn = SSHConnection(config)
                if new_conn.connect():
                    pool.append(new_conn)
                    self._stats["total_connections"] += 1
                    return new_conn
            
            # Pool is full, try to reuse oldest connection
            if pool:
                oldest_conn = min(pool, key=lambda c: c.last_used)
                if oldest_conn.is_alive():
                    return oldest_conn
            
            logger.warning(
                "No available SSH connections",
                client_id=client_id,
                pool_size=len(pool)
            )
            return None
    
    def execute_command(
        self,
        client_id: str,
        command: str,
        timeout: Optional[int] = None,
        retries: Optional[int] = None
    ) -> CommandResult:
        """Execute command on specified client with retry logic."""
        retries = retries or self.max_command_retries
        last_result = None
        
        for attempt in range(retries + 1):
            conn = self._get_connection(client_id)
            if not conn:
                last_result = CommandResult(
                    success=False,
                    stdout="",
                    stderr="No available SSH connections",
                    exit_code=-1,
                    execution_time=0.0,
                    command=command,
                    timestamp=datetime.now()
                )
                break
            
            result = conn.execute_command(command, timeout)
            self._stats["total_commands"] += 1
            
            if result.success:
                self._stats["successful_commands"] += 1
                return result
            else:
                self._stats["failed_commands"] += 1
                last_result = result
                
                if attempt < retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logger.warning(
                        "Command failed, retrying",
                        client_id=client_id,
                        attempt=attempt + 1,
                        wait_time=wait_time,
                        error=result.stderr
                    )
                    time.sleep(wait_time)
        
        logger.error(
            "Command failed after all retries",
            client_id=client_id,
            command=command[:100],
            retries=retries
        )
        
        return last_result or CommandResult(
            success=False,
            stdout="",
            stderr="Command failed after retries",
            exit_code=-1,
            execution_time=0.0,
            command=command,
            timestamp=datetime.now()
        )
    
    async def execute_command_async(
        self,
        client_id: str,
        command: str,
        timeout: Optional[int] = None,
        retries: Optional[int] = None
    ) -> CommandResult:
        """Execute command asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self.execute_command,
            client_id,
            command,
            timeout,
            retries
        )
    
    def execute_commands_batch(
        self,
        client_id: str,
        commands: List[str],
        timeout: Optional[int] = None,
        fail_fast: bool = True
    ) -> List[CommandResult]:
        """Execute multiple commands in sequence."""
        results = []
        
        for command in commands:
            result = self.execute_command(client_id, command, timeout)
            results.append(result)
            
            if fail_fast and not result.success:
                logger.warning(
                    "Batch execution stopped due to failure",
                    client_id=client_id,
                    failed_command=command[:100]
                )
                break
        
        return results
    
    def test_connection(self, client_id: str) -> bool:
        """Test SSH connection to client."""
        result = self.execute_command(client_id, "echo 'connection_test'")
        return result.success and "connection_test" in result.stdout
    
    def _start_health_monitor(self):
        """Start background health monitoring."""
        def health_check_worker():
            while True:
                try:
                    time.sleep(self.health_check_interval)
                    self._perform_health_checks()
                except Exception as e:
                    logger.error("Error in health check worker", error=str(e))
        
        health_thread = threading.Thread(target=health_check_worker, daemon=True)
        health_thread.start()
    
    def _perform_health_checks(self):
        """Perform health checks on all connections."""
        with self._pool_lock:
            cutoff_time = datetime.now() - timedelta(seconds=self.connection_idle_timeout)
            
            for client_id, pool in self._connection_pools.items():
                # Remove idle connections
                active_connections = []
                for conn in pool:
                    if conn.last_used > cutoff_time and conn.is_alive():
                        active_connections.append(conn)
                    else:
                        conn.close()
                        logger.info(
                            "Closed idle SSH connection",
                            client_id=client_id,
                            last_used=conn.last_used
                        )
                
                self._connection_pools[client_id] = active_connections
            
            # Update stats
            self._stats["active_connections"] = sum(
                len(pool) for pool in self._connection_pools.values()
            )
    
    def get_client_stats(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Get statistics for specific client."""
        with self._pool_lock:
            if client_id not in self._connection_pools:
                return None
            
            pool = self._connection_pools[client_id]
            config = self._client_configs[client_id]
            
            return {
                "client_id": client_id,
                "host": config.host,
                "port": config.port,
                "pool_size": len(pool),
                "max_pool_size": self.max_connections_per_client,
                "connections": [conn.get_stats() for conn in pool]
            }
    
    def get_overall_stats(self) -> Dict[str, Any]:
        """Get overall SSH manager statistics."""
        with self._pool_lock:
            return {
                "total_clients": len(self._client_configs),
                "total_connection_pools": len(self._connection_pools),
                **self._stats,
                "connection_pools": {
                    client_id: len(pool)
                    for client_id, pool in self._connection_pools.items()
                }
            }
    
    def close_all_connections(self):
        """Close all SSH connections (cleanup)."""
        with self._pool_lock:
            for pool in self._connection_pools.values():
                for conn in pool:
                    conn.close()
            
            self._connection_pools.clear()
            self._executor.shutdown(wait=True)
            
            logger.info("All SSH connections closed")
    
    def __del__(self):
        """Cleanup when manager is destroyed."""
        try:
            self.close_all_connections()
        except:
            pass
