"""
PostgreSQL MCP Client for monitoring and fixing PostgreSQL-related issues.
"""

import os
import subprocess
import time
from typing import Dict, List, Any, Optional
import structlog
import asyncpg
import psutil

from .base_client import ClientMCPServer

logger = structlog.get_logger(__name__)


class PostgresMCPClient(ClientMCPServer):
    """MCP Client for monitoring PostgreSQL database server."""
    
    def __init__(self, config_path: str):
        super().__init__(config_path)
        
        # PostgreSQL-specific configuration
        self.db_config = self.config.get("database", {})
        self.host = self.db_config.get("host", "localhost")
        self.port = self.db_config.get("port", 5432)
        self.database = self.db_config.get("database", "postgres")
        self.username = self.db_config.get("username", "postgres")
        self.password = self.db_config.get("password", "")
        
        # File paths
        self.data_directory = self.config.get("data_directory", "/var/lib/postgresql/data")
        self.config_file = self.config.get("config_file", "/etc/postgresql/postgresql.conf")
        self.log_directory = self.config.get("log_directory", "/var/log/postgresql")
        
        # Backup settings
        self.backup_enabled = self.config.get("backup_enabled", True)
        self.backup_dir = self.config.get("backup_dir", "/var/backups/postgresql")
        
        # Connection pool
        self.connection_pool: Optional[asyncpg.Pool] = None
        
        logger.info(
            "PostgreSQL client initialized",
            client_id=self.client_id,
            host=self.host,
            port=self.port,
            database=self.database
        )
    
    def get_log_paths(self) -> List[str]:
        """Return PostgreSQL log file paths to monitor."""
        log_paths = []
        
        # Standard log directory
        if os.path.exists(self.log_directory):
            for file in os.listdir(self.log_directory):
                if file.endswith('.log'):
                    log_paths.append(os.path.join(self.log_directory, file))
        
        # Common log locations
        common_paths = [
            "/var/log/postgresql/postgresql.log",
            "/var/log/postgresql/postgresql-*.log",
            f"{self.data_directory}/log/postgresql-*.log"
        ]
        
        for path in common_paths:
            if '*' in path:
                # Handle wildcards
                import glob
                log_paths.extend(glob.glob(path))
            elif os.path.exists(path):
                log_paths.append(path)
        
        # Add any additional log files specified in config
        additional_logs = self.config.get("additional_log_paths", [])
        log_paths.extend([path for path in additional_logs if os.path.exists(path)])
        
        return list(set(log_paths))  # Remove duplicates
    
    def get_service_name(self) -> str:
        """Return the service name for health monitoring."""
        return self.config.get("service_name", "postgresql")
    
    async def _get_db_connection(self) -> Optional[asyncpg.Connection]:
        """Get database connection."""
        try:
            if not self.connection_pool:
                self.connection_pool = await asyncpg.create_pool(
                    host=self.host,
                    port=self.port,
                    database=self.database,
                    user=self.username,
                    password=self.password,
                    min_size=1,
                    max_size=5,
                    timeout=30
                )
            
            return await self.connection_pool.acquire()
            
        except Exception as e:
            logger.error(
                "Failed to connect to PostgreSQL",
                client_id=self.client_id,
                error=str(e)
            )
            return None
    
    async def _release_connection(self, conn: asyncpg.Connection):
        """Release database connection back to pool."""
        try:
            if self.connection_pool and conn:
                await self.connection_pool.release(conn)
        except Exception as e:
            logger.error("Error releasing connection", error=str(e))
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate PostgreSQL configuration."""
        try:
            # Test database connection
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            async def test_connection():
                conn = await self._get_db_connection()
                if conn:
                    try:
                        await conn.fetchval("SELECT 1")
                        return True
                    finally:
                        await self._release_connection(conn)
                return False
            
            result = loop.run_until_complete(test_connection())
            loop.close()
            
            if result:
                logger.info("PostgreSQL connection validation passed", client_id=self.client_id)
                return True
            else:
                logger.error("PostgreSQL connection validation failed", client_id=self.client_id)
                return False
                
        except Exception as e:
            logger.error(
                "Error validating PostgreSQL configuration",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _backup_database(self) -> str:
        """Create backup of PostgreSQL database."""
        if not self.backup_enabled:
            return ""
        
        try:
            # Create backup directory if it doesn't exist
            os.makedirs(self.backup_dir, exist_ok=True)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(self.backup_dir, f"postgres_backup_{timestamp}.sql")
            
            # Create database dump
            env = os.environ.copy()
            if self.password:
                env['PGPASSWORD'] = self.password
            
            cmd = [
                "pg_dump",
                "-h", self.host,
                "-p", str(self.port),
                "-U", self.username,
                "-d", self.database,
                "-f", backup_path,
                "--verbose"
            ]
            
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                logger.info(
                    "PostgreSQL database backed up",
                    client_id=self.client_id,
                    backup_path=backup_path
                )
                return backup_path
            else:
                logger.error(
                    "Failed to backup PostgreSQL database",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return ""
                
        except Exception as e:
            logger.error(
                "Error backing up PostgreSQL database",
                client_id=self.client_id,
                error=str(e)
            )
            return ""
    
    def _restart_postgresql(self) -> bool:
        """Restart PostgreSQL service."""
        try:
            result = subprocess.run(
                ["systemctl", "restart", "postgresql"],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                logger.info("PostgreSQL restarted successfully", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Failed to restart PostgreSQL",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error restarting PostgreSQL",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _reload_postgresql(self) -> bool:
        """Reload PostgreSQL configuration."""
        try:
            result = subprocess.run(
                ["systemctl", "reload", "postgresql"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info("PostgreSQL configuration reloaded", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Failed to reload PostgreSQL configuration",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error reloading PostgreSQL configuration",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    async def _vacuum_database(self, full: bool = False) -> bool:
        """Perform database vacuum operation."""
        try:
            conn = await self._get_db_connection()
            if not conn:
                return False
            
            try:
                vacuum_cmd = "VACUUM FULL" if full else "VACUUM"
                await conn.execute(vacuum_cmd)
                
                logger.info(
                    "Database vacuum completed",
                    client_id=self.client_id,
                    full_vacuum=full
                )
                return True
                
            finally:
                await self._release_connection(conn)
                
        except Exception as e:
            logger.error(
                "Error performing database vacuum",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    async def _reindex_database(self) -> bool:
        """Reindex database."""
        try:
            conn = await self._get_db_connection()
            if not conn:
                return False
            
            try:
                await conn.execute("REINDEX DATABASE " + self.database)
                
                logger.info("Database reindexed", client_id=self.client_id)
                return True
                
            finally:
                await self._release_connection(conn)
                
        except Exception as e:
            logger.error(
                "Error reindexing database",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    async def _analyze_database(self) -> bool:
        """Update database statistics."""
        try:
            conn = await self._get_db_connection()
            if not conn:
                return False
            
            try:
                await conn.execute("ANALYZE")
                
                logger.info("Database statistics updated", client_id=self.client_id)
                return True
                
            finally:
                await self._release_connection(conn)
                
        except Exception as e:
            logger.error(
                "Error analyzing database",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    async def _kill_long_running_queries(self, max_duration_minutes: int = 60) -> bool:
        """Kill long-running queries."""
        try:
            conn = await self._get_db_connection()
            if not conn:
                return False
            
            try:
                # Find long-running queries
                query = """
                SELECT pid, query_start, query
                FROM pg_stat_activity
                WHERE state = 'active'
                AND query_start < NOW() - INTERVAL '%s minutes'
                AND query NOT LIKE '%%pg_stat_activity%%'
                """ % max_duration_minutes
                
                long_queries = await conn.fetch(query)
                
                killed_count = 0
                for row in long_queries:
                    try:
                        await conn.execute("SELECT pg_terminate_backend($1)", row['pid'])
                        killed_count += 1
                        
                        logger.warning(
                            "Killed long-running query",
                            client_id=self.client_id,
                            pid=row['pid'],
                            duration=str(row['query_start']),
                            query=row['query'][:200]
                        )
                    except:
                        pass  # Query might have finished naturally
                
                logger.info(
                    "Long-running queries killed",
                    client_id=self.client_id,
                    killed_count=killed_count
                )
                return True
                
            finally:
                await self._release_connection(conn)
                
        except Exception as e:
            logger.error(
                "Error killing long-running queries",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _fix_permissions(self) -> bool:
        """Fix PostgreSQL file permissions."""
        try:
            permission_fixes = [
                ["chown", "-R", "postgres:postgres", self.data_directory],
                ["chmod", "700", self.data_directory],
                ["chown", "postgres:postgres", self.config_file],
                ["chmod", "600", self.config_file]
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
            
            logger.info("PostgreSQL permissions fixed", client_id=self.client_id)
            return True
            
        except Exception as e:
            logger.error(
                "Error fixing PostgreSQL permissions",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def apply_fix(self, fix_commands: List[str]) -> Dict[str, Any]:
        """Apply fix commands to PostgreSQL service."""
        results = {
            "success": False,
            "commands_executed": [],
            "backup_created": "",
            "errors": []
        }
        
        try:
            # Create backup first
            backup_path = self._backup_database()
            results["backup_created"] = backup_path
            
            # Execute fix commands
            for command in fix_commands:
                command = command.strip()
                
                try:
                    if command == "systemctl restart postgresql":
                        success = self._restart_postgresql()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "PostgreSQL " + ("restarted" if success else "restart failed")
                        })
                        
                    elif command == "systemctl reload postgresql":
                        success = self._reload_postgresql()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "PostgreSQL " + ("reloaded" if success else "reload failed")
                        })
                        
                    elif command.upper().startswith("VACUUM"):
                        import asyncio
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        full_vacuum = "FULL" in command.upper()
                        success = loop.run_until_complete(self._vacuum_database(full_vacuum))
                        loop.close()
                        
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Database " + ("vacuumed" if success else "vacuum failed")
                        })
                        
                    elif command.upper().startswith("REINDEX"):
                        import asyncio
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        success = loop.run_until_complete(self._reindex_database())
                        loop.close()
                        
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Database " + ("reindexed" if success else "reindex failed")
                        })
                        
                    elif command.upper().startswith("ANALYZE"):
                        import asyncio
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        success = loop.run_until_complete(self._analyze_database())
                        loop.close()
                        
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Database " + ("analyzed" if success else "analyze failed")
                        })
                        
                    elif "kill" in command.lower() and "query" in command.lower():
                        import asyncio
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        success = loop.run_until_complete(self._kill_long_running_queries())
                        loop.close()
                        
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Long queries " + ("killed" if success else "kill failed")
                        })
                        
                    elif "chmod" in command or "chown" in command:
                        success = self._fix_permissions()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Permissions " + ("fixed" if success else "fix failed")
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
            
            self.stats["fixes_applied"] += 1
            
            logger.info(
                "PostgreSQL fix commands applied",
                client_id=self.client_id,
                success=results["success"],
                commands_count=len(fix_commands),
                errors_count=len(results["errors"])
            )
            
            return results
            
        except Exception as e:
            logger.error(
                "Error applying PostgreSQL fixes",
                client_id=self.client_id,
                error=str(e)
            )
            
            results["errors"].append(f"Fix application error: {str(e)}")
            return results
    
    async def get_database_status(self) -> Dict[str, Any]:
        """Get detailed PostgreSQL database status."""
        status = {
            "service_active": False,
            "connection_available": False,
            "database_size": 0,
            "active_connections": 0,
            "long_queries": 0,
            "version": "",
            "uptime": "",
            "cache_hit_ratio": 0.0,
            "error": None
        }
        
        try:
            # Check if service is active
            result = subprocess.run(
                ["systemctl", "is-active", "postgresql"],
                capture_output=True,
                text=True,
                timeout=10
            )
            status["service_active"] = (result.returncode == 0)
            
            # Test database connection and get stats
            conn = await self._get_db_connection()
            if conn:
                status["connection_available"] = True
                
                try:
                    # Get version
                    version = await conn.fetchval("SELECT version()")
                    status["version"] = version.split()[1] if version else "Unknown"
                    
                    # Get database size
                    db_size = await conn.fetchval(
                        "SELECT pg_database_size($1)", self.database
                    )
                    status["database_size"] = db_size
                    
                    # Get active connections
                    active_conns = await conn.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"
                    )
                    status["active_connections"] = active_conns
                    
                    # Get long running queries (>5 minutes)
                    long_queries = await conn.fetchval("""
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE state = 'active'
                        AND query_start < NOW() - INTERVAL '5 minutes'
                    """)
                    status["long_queries"] = long_queries
                    
                    # Get uptime
                    uptime = await conn.fetchval(
                        "SELECT date_trunc('second', current_timestamp - pg_postmaster_start_time())"
                    )
                    status["uptime"] = str(uptime) if uptime else ""
                    
                    # Get cache hit ratio
                    cache_stats = await conn.fetchrow("""
                        SELECT 
                            sum(heap_blks_hit) as hits,
                            sum(heap_blks_hit + heap_blks_read) as total
                        FROM pg_statio_user_tables
                    """)
                    
                    if cache_stats and cache_stats['total'] > 0:
                        status["cache_hit_ratio"] = (
                            cache_stats['hits'] / cache_stats['total'] * 100
                        )
                    
                finally:
                    await self._release_connection(conn)
            
        except Exception as e:
            status["error"] = str(e)
            logger.error(
                "Error getting PostgreSQL status",
                client_id=self.client_id,
                error=str(e)
            )
        
        return status
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive PostgreSQL client status."""
        base_status = super().get_status()
        
        # Get database status synchronously
        import asyncio
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            db_status = loop.run_until_complete(self.get_database_status())
            loop.close()
        except:
            db_status = {"error": "Failed to get database status"}
        
        base_status["postgresql_status"] = db_status
        base_status["configuration"] = {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "data_directory": self.data_directory,
            "config_file": self.config_file,
            "backup_enabled": self.backup_enabled
        }
        
        return base_status


def main():
    """Main entry point for PostgreSQL MCP client."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python postgres_client.py <config_path>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    client = PostgresMCPClient(config_path)
    
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("PostgreSQL client interrupted by user")
    except Exception as e:
        logger.error("PostgreSQL client error", error=str(e))


if __name__ == "__main__":
    main()
