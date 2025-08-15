"""
Redis MCP Client for monitoring and fixing Redis-related issues.
"""

import os
import subprocess
import time
import json
import glob
from typing import Dict, List, Any, Optional
import structlog
import redis
import psutil

from .base_client import ClientMCPServer

logger = structlog.get_logger(__name__)


class RedisMCPClient(ClientMCPServer):
    """MCP Client for monitoring Redis server."""
    
    def __init__(self, config_path: str):
        super().__init__(config_path)
        
        # Redis-specific configuration
        self.redis_config = self.config.get("redis", {})
        self.host = self.redis_config.get("host", "localhost")
        self.port = self.redis_config.get("port", 6379)
        self.password = self.redis_config.get("password", None)
        self.database = self.redis_config.get("database", 0)
        self.cluster_mode = self.redis_config.get("cluster_mode", False)
        self.cluster_nodes = self.redis_config.get("cluster_nodes", [])
        
        # File paths
        self.config_file = self.config.get("config_file", "/etc/redis/redis.conf")
        self.log_directory = self.config.get("log_directory", "/var/log/redis")
        self.data_directory = self.config.get("data_directory", "/var/lib/redis")
        self.pid_file = self.config.get("pid_file", "/var/run/redis/redis-server.pid")
        
        # Backup settings
        self.backup_enabled = self.config.get("backup_enabled", True)
        self.backup_dir = self.config.get("backup_dir", "/var/backups/redis")
        
        # Memory settings
        self.max_memory_warning = self.config.get("max_memory_warning_percent", 80)
        self.max_memory_critical = self.config.get("max_memory_critical_percent", 90)
        
        # Redis connections
        self.redis_client: Optional[redis.Redis] = None
        self.cluster_client: Optional[redis.RedisCluster] = None
        
        logger.info(
            "Redis client initialized",
            client_id=self.client_id,
            host=self.host,
            port=self.port,
            cluster_mode=self.cluster_mode
        )
    
    def get_log_paths(self) -> List[str]:
        """Return Redis log file paths to monitor."""
        log_paths = []
        
        # Standard log directory
        if os.path.exists(self.log_directory):
            for file in os.listdir(self.log_directory):
                if file.endswith('.log'):
                    log_paths.append(os.path.join(self.log_directory, file))
        
        # Common log locations
        common_paths = [
            "/var/log/redis/redis-server.log",
            "/var/log/redis/redis.log",
            "/var/log/redis-server.log",
            "/var/log/redis.log",
            f"{self.data_directory}/redis-server.log"
        ]
        
        for path in common_paths:
            if '*' in path:
                log_paths.extend(glob.glob(path))
            elif os.path.exists(path):
                log_paths.append(path)
        
        # Add any additional log files specified in config
        additional_logs = self.config.get("additional_log_paths", [])
        log_paths.extend([path for path in additional_logs if os.path.exists(path)])
        
        return list(set(log_paths))  # Remove duplicates
    
    def get_service_name(self) -> str:
        """Return the service name for health monitoring."""
        return self.config.get("service_name", "redis-server")
    
    def _get_redis_client(self) -> Optional[redis.Redis]:
        """Get Redis client connection."""
        try:
            if not self.redis_client:
                if self.cluster_mode:
                    # Cluster mode
                    startup_nodes = []
                    for node in self.cluster_nodes:
                        startup_nodes.append({
                            "host": node.get("host", "localhost"),
                            "port": node.get("port", 6379)
                        })
                    
                    self.cluster_client = redis.RedisCluster(
                        startup_nodes=startup_nodes,
                        password=self.password,
                        decode_responses=True,
                        skip_full_coverage_check=True,
                        socket_timeout=30
                    )
                    return self.cluster_client
                else:
                    # Single instance mode
                    self.redis_client = redis.Redis(
                        host=self.host,
                        port=self.port,
                        password=self.password,
                        db=self.database,
                        decode_responses=True,
                        socket_timeout=30,
                        socket_connect_timeout=30
                    )
                    return self.redis_client
            
            return self.redis_client or self.cluster_client
            
        except Exception as e:
            logger.error(
                "Failed to connect to Redis",
                client_id=self.client_id,
                error=str(e)
            )
            return None
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate Redis configuration."""
        try:
            # Test Redis connection
            client = self._get_redis_client()
            if not client:
                return False
            
            # Test basic operations
            test_key = "mcp_health_check"
            client.set(test_key, "test", ex=10)
            result = client.get(test_key)
            client.delete(test_key)
            
            if result == "test":
                logger.info("Redis connection validation passed", client_id=self.client_id)
                return True
            else:
                logger.error("Redis connection validation failed", client_id=self.client_id)
                return False
                
        except Exception as e:
            logger.error(
                "Error validating Redis configuration",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _backup_redis_data(self) -> str:
        """Create backup of Redis data."""
        if not self.backup_enabled:
            return ""
        
        try:
            # Create backup directory if it doesn't exist
            os.makedirs(self.backup_dir, exist_ok=True)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(self.backup_dir, f"redis_backup_{timestamp}.rdb")
            
            # Force Redis to save current data
            client = self._get_redis_client()
            if client:
                client.bgsave()
                
                # Wait for background save to complete
                while client.lastsave() == client.lastsave():
                    time.sleep(1)
                    break
                
                # Copy RDB file to backup location
                rdb_file = os.path.join(self.data_directory, "dump.rdb")
                if os.path.exists(rdb_file):
                    subprocess.run([
                        "cp", rdb_file, backup_path
                    ], check=True, timeout=60)
                    
                    logger.info(
                        "Redis data backed up",
                        client_id=self.client_id,
                        backup_path=backup_path
                    )
                    
                    return backup_path
            
            return ""
                
        except Exception as e:
            logger.error(
                "Error backing up Redis data",
                client_id=self.client_id,
                error=str(e)
            )
            return ""
    
    def _restart_redis(self) -> bool:
        """Restart Redis service."""
        try:
            result = subprocess.run(
                ["systemctl", "restart", "redis-server"],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                logger.info("Redis restarted successfully", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Failed to restart Redis",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error restarting Redis",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _flush_redis_cache(self, flush_all: bool = False) -> bool:
        """Flush Redis cache."""
        try:
            client = self._get_redis_client()
            if not client:
                return False
            
            if flush_all:
                client.flushall()
                logger.info("All Redis databases flushed", client_id=self.client_id)
            else:
                client.flushdb()
                logger.info("Current Redis database flushed", client_id=self.client_id)
            
            return True
            
        except Exception as e:
            logger.error(
                "Error flushing Redis cache",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _increase_memory_limit(self, new_limit: str) -> bool:
        """Increase Redis memory limit."""
        try:
            client = self._get_redis_client()
            if not client:
                return False
            
            # Set maxmemory configuration
            client.config_set("maxmemory", new_limit)
            
            logger.info(
                "Redis memory limit increased",
                client_id=self.client_id,
                new_limit=new_limit
            )
            return True
            
        except Exception as e:
            logger.error(
                "Error increasing Redis memory limit",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _set_memory_policy(self, policy: str = "allkeys-lru") -> bool:
        """Set Redis memory eviction policy."""
        try:
            client = self._get_redis_client()
            if not client:
                return False
            
            client.config_set("maxmemory-policy", policy)
            
            logger.info(
                "Redis memory policy updated",
                client_id=self.client_id,
                policy=policy
            )
            return True
            
        except Exception as e:
            logger.error(
                "Error setting Redis memory policy",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _fix_persistence_issues(self) -> bool:
        """Fix Redis persistence issues."""
        try:
            # Check and fix permissions
            permission_fixes = [
                ["chown", "-R", "redis:redis", self.data_directory],
                ["chmod", "755", self.data_directory],
                ["chown", "redis:redis", self.config_file],
                ["chmod", "640", self.config_file]
            ]
            
            for cmd in permission_fixes:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode != 0:
                    logger.warning(
                        "Permission fix command failed",
                        client_id=self.client_id,
                        command=" ".join(cmd),
                        error=result.stderr.decode() if result.stderr else ""
                    )
            
            # Force a save to test persistence
            client = self._get_redis_client()
            if client:
                client.save()
                logger.info("Redis persistence test successful", client_id=self.client_id)
            
            return True
            
        except Exception as e:
            logger.error(
                "Error fixing Redis persistence issues",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _kill_slow_clients(self) -> bool:
        """Kill slow Redis clients."""
        try:
            client = self._get_redis_client()
            if not client:
                return False
            
            # Get client list
            clients = client.client_list()
            killed_count = 0
            
            for client_info in clients:
                # Kill clients that have been idle too long or consuming too much memory
                if (client_info.get('idle', 0) > 3600 or  # 1 hour idle
                    client_info.get('omem', 0) > 100 * 1024 * 1024):  # 100MB output buffer
                    try:
                        client.client_kill_filter(
                            _id=client_info.get('id')
                        )
                        killed_count += 1
                        
                        logger.warning(
                            "Killed slow Redis client",
                            client_id=self.client_id,
                            client_id_killed=client_info.get('id'),
                            idle_time=client_info.get('idle', 0),
                            memory_usage=client_info.get('omem', 0)
                        )
                    except:
                        pass  # Client might have disconnected naturally
            
            logger.info(
                "Slow Redis clients killed",
                client_id=self.client_id,
                killed_count=killed_count
            )
            return True
            
        except Exception as e:
            logger.error(
                "Error killing slow Redis clients",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def apply_fix(self, fix_commands: List[str]) -> Dict[str, Any]:
        """Apply fix commands to Redis service."""
        results = {
            "success": False,
            "commands_executed": [],
            "backup_created": "",
            "errors": []
        }
        
        try:
            # Create backup first
            backup_path = self._backup_redis_data()
            results["backup_created"] = backup_path
            
            # Execute fix commands
            for command in fix_commands:
                command = command.strip()
                
                try:
                    if command == "systemctl restart redis-server":
                        success = self._restart_redis()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Redis " + ("restarted" if success else "restart failed")
                        })
                        
                    elif "flushall" in command.lower():
                        success = self._flush_redis_cache(flush_all=True)
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "All Redis databases " + ("flushed" if success else "flush failed")
                        })
                        
                    elif "flushdb" in command.lower():
                        success = self._flush_redis_cache(flush_all=False)
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Current Redis database " + ("flushed" if success else "flush failed")
                        })
                        
                    elif "maxmemory" in command.lower():
                        # Extract memory limit from command
                        parts = command.split()
                        memory_limit = "1gb"  # Default
                        for i, part in enumerate(parts):
                            if "maxmemory" in part.lower() and i + 1 < len(parts):
                                memory_limit = parts[i + 1]
                                break
                        
                        success = self._increase_memory_limit(memory_limit)
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": f"Memory limit {'set to ' + memory_limit if success else 'update failed'}"
                        })
                        
                    elif "memory-policy" in command.lower():
                        # Extract policy from command
                        policy = "allkeys-lru"  # Default
                        if "allkeys-lfu" in command.lower():
                            policy = "allkeys-lfu"
                        elif "volatile-lru" in command.lower():
                            policy = "volatile-lru"
                        elif "volatile-lfu" in command.lower():
                            policy = "volatile-lfu"
                        
                        success = self._set_memory_policy(policy)
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": f"Memory policy {'set to ' + policy if success else 'update failed'}"
                        })
                        
                    elif "kill" in command.lower() and "client" in command.lower():
                        success = self._kill_slow_clients()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Slow clients " + ("killed" if success else "kill failed")
                        })
                        
                    elif "persistence" in command.lower() or ("chmod" in command or "chown" in command):
                        success = self._fix_persistence_issues()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Persistence issues " + ("fixed" if success else "fix failed")
                        })
                        
                    else:
                        # Generic command execution
                        result = subprocess.run(
                            command.split(),
                            capture_output=True,
                            text=True,
                            timeout=300
                        )
                        
                        success = (result.returncode == 0)
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": result.stdout if success else result.stderr,
                            "exit_code": result.returncode
                        })
                    
                    if not success:
                        results["errors"].append(f"Command failed: {command}")
                        if self.config.get("fail_fast", True):
                            break
                            
                except subprocess.TimeoutExpired:
                    error_msg = f"Command timed out: {command}"
                    results["errors"].append(error_msg)
                    results["commands_executed"].append({
                        "command": command,
                        "success": False,
                        "output": "Command timed out"
                    })
                    
                    if self.config.get("fail_fast", True):
                        break
                        
                except Exception as e:
                    error_msg = f"Command error: {command} - {str(e)}"
                    results["errors"].append(error_msg)
                    results["commands_executed"].append({
                        "command": command,
                        "success": False,
                        "output": str(e)
                    })
                    
                    if self.config.get("fail_fast", True):
                        break
            
            # Determine overall success
            results["success"] = (len(results["errors"]) == 0)
            
            # Final validation after all fixes
            if results["success"]:
                if not self.validate_config({}):
                    results["success"] = False
                    results["errors"].append("Final Redis validation failed")
            
            self.stats["fixes_applied"] += 1
            
            logger.info(
                "Redis fix commands applied",
                client_id=self.client_id,
                success=results["success"],
                commands_count=len(fix_commands),
                errors_count=len(results["errors"])
            )
            
            return results
            
        except Exception as e:
            logger.error(
                "Error applying Redis fixes",
                client_id=self.client_id,
                error=str(e)
            )
            
            results["errors"].append(f"Fix application error: {str(e)}")
            return results
    
    def get_redis_status(self) -> Dict[str, Any]:
        """Get detailed Redis status information."""
        status = {
            "service_active": False,
            "connection_available": False,
            "memory_usage": {},
            "keyspace_info": {},
            "persistence_info": {},
            "replication_info": {},
            "version": "",
            "uptime_seconds": 0,
            "connected_clients": 0,
            "blocked_clients": 0,
            "error": None
        }
        
        try:
            # Check if service is active
            result = subprocess.run(
                ["systemctl", "is-active", "redis-server"],
                capture_output=True,
                text=True,
                timeout=10
            )
            status["service_active"] = (result.returncode == 0)
            
            # Test Redis connection and get info
            client = self._get_redis_client()
            if client:
                status["connection_available"] = True
                
                # Get Redis info
                info = client.info()
                
                # Extract key metrics
                status["version"] = info.get("redis_version", "Unknown")
                status["uptime_seconds"] = info.get("uptime_in_seconds", 0)
                status["connected_clients"] = info.get("connected_clients", 0)
                status["blocked_clients"] = info.get("blocked_clients", 0)
                
                # Memory information
                status["memory_usage"] = {
                    "used_memory": info.get("used_memory", 0),
                    "used_memory_human": info.get("used_memory_human", "0B"),
                    "used_memory_peak": info.get("used_memory_peak", 0),
                    "used_memory_peak_human": info.get("used_memory_peak_human", "0B"),
                    "maxmemory": info.get("maxmemory", 0),
                    "maxmemory_human": info.get("maxmemory_human", "0B"),
                    "maxmemory_policy": info.get("maxmemory_policy", "noeviction")
                }
                
                # Keyspace information
                keyspace = {}
                for key, value in info.items():
                    if key.startswith("db"):
                        keyspace[key] = value
                status["keyspace_info"] = keyspace
                
                # Persistence information
                status["persistence_info"] = {
                    "rdb_changes_since_last_save": info.get("rdb_changes_since_last_save", 0),
                    "rdb_last_save_time": info.get("rdb_last_save_time", 0),
                    "rdb_last_bgsave_status": info.get("rdb_last_bgsave_status", "unknown"),
                    "aof_enabled": info.get("aof_enabled", 0),
                    "aof_rewrite_in_progress": info.get("aof_rewrite_in_progress", 0)
                }
                
                # Replication information
                status["replication_info"] = {
                    "role": info.get("role", "unknown"),
                    "connected_slaves": info.get("connected_slaves", 0),
                    "master_repl_offset": info.get("master_repl_offset", 0)
                }
            
        except Exception as e:
            status["error"] = str(e)
            logger.error(
                "Error getting Redis status",
                client_id=self.client_id,
                error=str(e)
            )
        
        return status
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive Redis client status."""
        base_status = super().get_status()
        base_status["redis_status"] = self.get_redis_status()
        base_status["configuration"] = {
            "host": self.host,
            "port": self.port,
            "cluster_mode": self.cluster_mode,
            "config_file": self.config_file,
            "data_directory": self.data_directory,
            "backup_enabled": self.backup_enabled
        }
        
        return base_status


def main():
    """Main entry point for Redis MCP client."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python redis_client.py <config_path>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    client = RedisMCPClient(config_path)
    
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Redis client interrupted by user")
    except Exception as e:
        logger.error("Redis client error", error=str(e))


if __name__ == "__main__":
    main()
