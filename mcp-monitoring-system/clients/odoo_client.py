"""
Odoo MCP Client for monitoring and fixing Odoo-related issues.
"""

import os
import subprocess
import time
import json
import glob
import requests
import psycopg2
from typing import Dict, List, Any, Optional
import structlog
import psutil

from .base_client import ClientMCPServer

logger = structlog.get_logger(__name__)


class OdooMCPClient(ClientMCPServer):
    """MCP Client for monitoring Odoo ERP system."""
    
    def __init__(self, config_path: str):
        super().__init__(config_path)
        
        # Odoo-specific configuration
        self.odoo_config = self.config.get("odoo", {})
        self.odoo_version = self.odoo_config.get("version", "16.0")
        self.odoo_edition = self.odoo_config.get("edition", "community")  # community or enterprise
        self.odoo_path = self.odoo_config.get("path", "/opt/odoo")
        self.odoo_config_file = self.odoo_config.get("config_file", "/etc/odoo/odoo.conf")
        self.odoo_user = self.odoo_config.get("user", "odoo")
        self.addons_path = self.odoo_config.get("addons_path", "/opt/odoo/addons")
        
        # Web service configuration
        self.web_host = self.odoo_config.get("web_host", "localhost")
        self.web_port = self.odoo_config.get("web_port", 8069)
        self.web_url = f"http://{self.web_host}:{self.web_port}"
        
        # Database configuration
        self.db_config = self.config.get("database", {})
        self.db_host = self.db_config.get("host", "localhost")
        self.db_port = self.db_config.get("port", 5432)
        self.db_name = self.db_config.get("name", "odoo")
        self.db_user = self.db_config.get("user", "odoo")
        self.db_password = self.db_config.get("password", "")
        
        # File paths
        self.log_directory = self.config.get("log_directory", "/var/log/odoo")
        self.data_directory = self.config.get("data_directory", "/var/lib/odoo")
        self.pid_file = self.config.get("pid_file", "/var/run/odoo/odoo.pid")
        
        # Backup settings
        self.backup_enabled = self.config.get("backup_enabled", True)
        self.backup_dir = self.config.get("backup_dir", "/var/backups/odoo")
        
        # Performance thresholds
        self.memory_warning = self.config.get("memory_warning_mb", 1024)
        self.memory_critical = self.config.get("memory_critical_mb", 2048)
        self.cpu_warning = self.config.get("cpu_warning_percent", 80)
        
        # Database connection
        self.db_connection: Optional[psycopg2.connection] = None
        
        logger.info(
            "Odoo client initialized",
            client_id=self.client_id,
            version=self.odoo_version,
            edition=self.odoo_edition,
            web_url=self.web_url
        )
    
    def get_log_paths(self) -> List[str]:
        """Return Odoo log file paths to monitor."""
        log_paths = []
        
        # Standard log directory
        if os.path.exists(self.log_directory):
            for file in os.listdir(self.log_directory):
                if file.endswith('.log'):
                    log_paths.append(os.path.join(self.log_directory, file))
        
        # Common log locations
        common_paths = [
            "/var/log/odoo/odoo-server.log",
            "/var/log/odoo/odoo.log",
            "/var/log/odoo-server.log",
            f"{self.odoo_path}/odoo.log",
            f"{self.data_directory}/odoo.log"
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
        return self.config.get("service_name", "odoo")
    
    def _get_db_connection(self) -> Optional[psycopg2.connection]:
        """Get database connection."""
        try:
            if not self.db_connection or self.db_connection.closed:
                self.db_connection = psycopg2.connect(
                    host=self.db_host,
                    port=self.db_port,
                    database=self.db_name,
                    user=self.db_user,
                    password=self.db_password,
                    connect_timeout=30
                )
                self.db_connection.autocommit = True
            
            return self.db_connection
            
        except Exception as e:
            logger.error(
                "Failed to connect to Odoo database",
                client_id=self.client_id,
                error=str(e)
            )
            return None
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate Odoo configuration."""
        try:
            # Test database connection
            conn = self._get_db_connection()
            if not conn:
                return False
            
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()
                
                if result and result[0] == 1:
                    logger.info("Odoo database connection validation passed", client_id=self.client_id)
                else:
                    logger.error("Odoo database connection validation failed", client_id=self.client_id)
                    return False
            
            # Test web service
            try:
                response = requests.get(
                    f"{self.web_url}/web/database/selector",
                    timeout=30
                )
                if response.status_code in [200, 303]:  # 303 is redirect to login
                    logger.info("Odoo web service validation passed", client_id=self.client_id)
                    return True
                else:
                    logger.warning(
                        "Odoo web service returned unexpected status",
                        client_id=self.client_id,
                        status_code=response.status_code
                    )
                    return False
            except requests.exceptions.RequestException:
                logger.warning("Odoo web service not accessible", client_id=self.client_id)
                return False
                
        except Exception as e:
            logger.error(
                "Error validating Odoo configuration",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _backup_odoo_data(self) -> str:
        """Create backup of Odoo data."""
        if not self.backup_enabled:
            return ""
        
        try:
            # Create backup directory if it doesn't exist
            os.makedirs(self.backup_dir, exist_ok=True)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            
            # Backup database
            db_backup_path = os.path.join(self.backup_dir, f"odoo_db_{timestamp}.sql")
            
            env = os.environ.copy()
            if self.db_password:
                env['PGPASSWORD'] = self.db_password
            
            cmd = [
                "pg_dump",
                "-h", self.db_host,
                "-p", str(self.db_port),
                "-U", self.db_user,
                "-d", self.db_name,
                "-f", db_backup_path,
                "--no-owner",
                "--verbose"
            ]
            
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=600  # 10 minutes for large databases
            )
            
            if result.returncode != 0:
                logger.error(
                    "Failed to backup Odoo database",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return ""
            
            # Backup filestore
            filestore_backup_path = os.path.join(self.backup_dir, f"odoo_filestore_{timestamp}.tar.gz")
            filestore_path = os.path.join(self.data_directory, "filestore", self.db_name)
            
            if os.path.exists(filestore_path):
                subprocess.run([
                    "tar", "-czf", filestore_backup_path,
                    "-C", os.path.dirname(filestore_path),
                    os.path.basename(filestore_path)
                ], check=True, timeout=300)
            
            logger.info(
                "Odoo data backed up",
                client_id=self.client_id,
                db_backup=db_backup_path,
                filestore_backup=filestore_backup_path
            )
            
            return db_backup_path
                
        except Exception as e:
            logger.error(
                "Error backing up Odoo data",
                client_id=self.client_id,
                error=str(e)
            )
            return ""
    
    def _restart_odoo(self) -> bool:
        """Restart Odoo service."""
        try:
            result = subprocess.run(
                ["systemctl", "restart", "odoo"],
                capture_output=True,
                text=True,
                timeout=120  # Odoo can take time to restart
            )
            
            if result.returncode == 0:
                logger.info("Odoo restarted successfully", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Failed to restart Odoo",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error restarting Odoo",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _clear_odoo_cache(self) -> bool:
        """Clear Odoo cache and assets."""
        try:
            # Clear web assets cache
            assets_path = os.path.join(self.data_directory, "web_assets")
            if os.path.exists(assets_path):
                subprocess.run([
                    "find", assets_path, "-type", "f", "-delete"
                ], timeout=60)
            
            # Clear sessions
            sessions_path = os.path.join(self.data_directory, "sessions")
            if os.path.exists(sessions_path):
                subprocess.run([
                    "find", sessions_path, "-type", "f", "-delete"
                ], timeout=60)
            
            # Clear database cache using SQL
            conn = self._get_db_connection()
            if conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM ir_cache")
                    cursor.execute("DELETE FROM bus_bus")
                    cursor.execute("DELETE FROM ir_attachment WHERE res_model='ir.ui.view'")
            
            logger.info("Odoo cache cleared", client_id=self.client_id)
            return True
            
        except Exception as e:
            logger.error(
                "Error clearing Odoo cache",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _update_modules(self, modules: List[str] = None) -> bool:
        """Update Odoo modules."""
        try:
            cmd = [
                "python3",
                os.path.join(self.odoo_path, "odoo-bin"),
                "-c", self.odoo_config_file,
                "-d", self.db_name,
                "--stop-after-init"
            ]
            
            if modules:
                cmd.extend(["-u", ",".join(modules)])
            else:
                cmd.append("--update=all")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=self.odoo_path
            )
            
            if result.returncode == 0:
                logger.info(
                    "Odoo modules updated",
                    client_id=self.client_id,
                    modules=modules or ["all"]
                )
                return True
            else:
                logger.error(
                    "Failed to update Odoo modules",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error updating Odoo modules",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _fix_database_locks(self) -> bool:
        """Fix database locks and kill blocking queries."""
        try:
            conn = self._get_db_connection()
            if not conn:
                return False
            
            with conn.cursor() as cursor:
                # Find blocking queries
                cursor.execute("""
                    SELECT 
                        blocked_locks.pid AS blocked_pid,
                        blocked_activity.usename AS blocked_user,
                        blocking_locks.pid AS blocking_pid,
                        blocking_activity.usename AS blocking_user,
                        blocked_activity.query AS blocked_statement,
                        blocking_activity.query AS blocking_statement
                    FROM pg_catalog.pg_locks blocked_locks
                    JOIN pg_catalog.pg_stat_activity blocked_activity 
                        ON blocked_activity.pid = blocked_locks.pid
                    JOIN pg_catalog.pg_locks blocking_locks 
                        ON blocking_locks.locktype = blocked_locks.locktype
                        AND blocking_locks.DATABASE IS NOT DISTINCT FROM blocked_locks.DATABASE
                        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
                        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
                        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
                        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
                        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
                        AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
                        AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
                        AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
                        AND blocking_locks.pid != blocked_locks.pid
                    JOIN pg_catalog.pg_stat_activity blocking_activity 
                        ON blocking_activity.pid = blocking_locks.pid
                    WHERE NOT blocked_locks.GRANTED
                """)
                
                blocking_queries = cursor.fetchall()
                killed_count = 0
                
                for row in blocking_queries:
                    blocking_pid = row[2]
                    try:
                        cursor.execute("SELECT pg_terminate_backend(%s)", (blocking_pid,))
                        killed_count += 1
                        
                        logger.warning(
                            "Killed blocking query",
                            client_id=self.client_id,
                            pid=blocking_pid,
                            user=row[3],
                            query=row[5][:200]
                        )
                    except:
                        pass  # Query might have finished naturally
                
                logger.info(
                    "Database locks fixed",
                    client_id=self.client_id,
                    killed_count=killed_count
                )
                return True
                
        except Exception as e:
            logger.error(
                "Error fixing database locks",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _fix_permissions(self) -> bool:
        """Fix Odoo file permissions."""
        try:
            permission_fixes = [
                ["chown", "-R", f"{self.odoo_user}:{self.odoo_user}", self.data_directory],
                ["chown", "-R", f"{self.odoo_user}:{self.odoo_user}", self.addons_path],
                ["chown", f"{self.odoo_user}:{self.odoo_user}", self.odoo_config_file],
                ["chmod", "-R", "755", self.data_directory],
                ["chmod", "640", self.odoo_config_file]
            ]
            
            for cmd in permission_fixes:
                result = subprocess.run(cmd, capture_output=True, timeout=60)
                if result.returncode != 0:
                    logger.warning(
                        "Permission fix command failed",
                        client_id=self.client_id,
                        command=" ".join(cmd),
                        error=result.stderr.decode() if result.stderr else ""
                    )
            
            logger.info("Odoo permissions fixed", client_id=self.client_id)
            return True
            
        except Exception as e:
            logger.error(
                "Error fixing Odoo permissions",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _vacuum_database(self) -> bool:
        """Vacuum Odoo database."""
        try:
            conn = self._get_db_connection()
            if not conn:
                return False
            
            with conn.cursor() as cursor:
                cursor.execute("VACUUM ANALYZE")
            
            logger.info("Odoo database vacuumed", client_id=self.client_id)
            return True
            
        except Exception as e:
            logger.error(
                "Error vacuuming Odoo database",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def apply_fix(self, fix_commands: List[str]) -> Dict[str, Any]:
        """Apply fix commands to Odoo service."""
        results = {
            "success": False,
            "commands_executed": [],
            "backup_created": "",
            "errors": []
        }
        
        try:
            # Create backup first
            backup_path = self._backup_odoo_data()
            results["backup_created"] = backup_path
            
            # Execute fix commands
            for command in fix_commands:
                command = command.strip()
                
                try:
                    if command == "systemctl restart odoo":
                        success = self._restart_odoo()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Odoo " + ("restarted" if success else "restart failed")
                        })
                        
                    elif "clear cache" in command.lower():
                        success = self._clear_odoo_cache()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Cache " + ("cleared" if success else "clear failed")
                        })
                        
                    elif "update modules" in command.lower():
                        # Extract module names if specified
                        modules = None
                        if ":" in command:
                            module_part = command.split(":", 1)[1].strip()
                            modules = [m.strip() for m in module_part.split(",")]
                        
                        success = self._update_modules(modules)
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Modules " + ("updated" if success else "update failed")
                        })
                        
                    elif "fix locks" in command.lower():
                        success = self._fix_database_locks()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Database locks " + ("fixed" if success else "fix failed")
                        })
                        
                    elif "vacuum" in command.lower():
                        success = self._vacuum_database()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Database " + ("vacuumed" if success else "vacuum failed")
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
            
            # Final validation after all fixes
            if results["success"]:
                if not self.validate_config({}):
                    results["success"] = False
                    results["errors"].append("Final Odoo validation failed")
            
            self.stats["fixes_applied"] += 1
            
            logger.info(
                "Odoo fix commands applied",
                client_id=self.client_id,
                success=results["success"],
                commands_count=len(fix_commands),
                errors_count=len(results["errors"])
            )
            
            return results
            
        except Exception as e:
            logger.error(
                "Error applying Odoo fixes",
                client_id=self.client_id,
                error=str(e)
            )
            
            results["errors"].append(f"Fix application error: {str(e)}")
            return results
    
    def get_odoo_status(self) -> Dict[str, Any]:
        """Get detailed Odoo status information."""
        status = {
            "service_active": False,
            "web_accessible": False,
            "database_accessible": False,
            "memory_usage_mb": 0,
            "cpu_usage_percent": 0,
            "worker_processes": 0,
            "active_sessions": 0,
            "database_size_mb": 0,
            "version": self.odoo_version,
            "edition": self.odoo_edition,
            "modules_count": 0,
            "error": None
        }
        
        try:
            # Check if service is active
            result = subprocess.run(
                ["systemctl", "is-active", "odoo"],
                capture_output=True,
                text=True,
                timeout=10
            )
            status["service_active"] = (result.returncode == 0)
            
            # Test web accessibility
            try:
                response = requests.get(f"{self.web_url}/web/health", timeout=10)
                status["web_accessible"] = (response.status_code == 200)
            except:
                status["web_accessible"] = False
            
            # Test database connectivity
            conn = self._get_db_connection()
            if conn:
                status["database_accessible"] = True
                
                with conn.cursor() as cursor:
                    # Get database size
                    cursor.execute("SELECT pg_database_size(%s)", (self.db_name,))
                    db_size = cursor.fetchone()[0]
                    status["database_size_mb"] = db_size // (1024 * 1024)
                    
                    # Get active sessions
                    cursor.execute("""
                        SELECT count(*) FROM ir_sessions 
                        WHERE expiration_date > current_timestamp
                    """)
                    status["active_sessions"] = cursor.fetchone()[0]
                    
                    # Get modules count
                    cursor.execute("SELECT count(*) FROM ir_module_module WHERE state = 'installed'")
                    status["modules_count"] = cursor.fetchone()[0]
            
            # Get process information
            if os.path.exists(self.pid_file):
                try:
                    with open(self.pid_file, 'r') as f:
                        master_pid = int(f.read().strip())
                    
                    master_process = psutil.Process(master_pid)
                    
                    # Get memory usage
                    memory_info = master_process.memory_info()
                    status["memory_usage_mb"] = memory_info.rss // (1024 * 1024)
                    
                    # Get CPU usage
                    status["cpu_usage_percent"] = master_process.cpu_percent(interval=1)
                    
                    # Count worker processes
                    children = master_process.children(recursive=True)
                    status["worker_processes"] = len(children)
                    
                except (psutil.NoSuchProcess, ValueError):
                    pass
            
        except Exception as e:
            status["error"] = str(e)
            logger.error(
                "Error getting Odoo status",
                client_id=self.client_id,
                error=str(e)
            )
        
        return status
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive Odoo client status."""
        base_status = super().get_status()
        base_status["odoo_status"] = self.get_odoo_status()
        base_status["configuration"] = {
            "version": self.odoo_version,
            "edition": self.odoo_edition,
            "web_url": self.web_url,
            "database": self.db_name,
            "config_file": self.odoo_config_file,
            "data_directory": self.data_directory,
            "backup_enabled": self.backup_enabled
        }
        
        return base_status


def main():
    """Main entry point for Odoo MCP client."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python odoo_client.py <config_path>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    client = OdooMCPClient(config_path)
    
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Odoo client interrupted by user")
    except Exception as e:
        logger.error("Odoo client error", error=str(e))


if __name__ == "__main__":
    main()
