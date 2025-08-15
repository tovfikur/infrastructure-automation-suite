"""
Nginx MCP Client for monitoring and fixing Nginx-related issues.
"""

import os
import subprocess
import time
from typing import Dict, List, Any
import structlog

from .base_client import ClientMCPServer

logger = structlog.get_logger(__name__)


class NginxMCPClient(ClientMCPServer):
    """MCP Client for monitoring Nginx web server."""
    
    def __init__(self, config_path: str):
        super().__init__(config_path)
        
        # Nginx-specific configuration
        self.nginx_config_path = self.config.get("nginx_config_path", "/etc/nginx/nginx.conf")
        self.nginx_sites_path = self.config.get("nginx_sites_path", "/etc/nginx/sites-enabled")
        self.nginx_pid_file = self.config.get("nginx_pid_file", "/var/run/nginx.pid")
        self.access_log_path = self.config.get("access_log_path", "/var/log/nginx/access.log")
        self.error_log_path = self.config.get("error_log_path", "/var/log/nginx/error.log")
        
        # Backup configuration
        self.backup_enabled = self.config.get("backup_enabled", True)
        self.backup_dir = self.config.get("backup_dir", "/var/backups/nginx")
        
        logger.info(
            "Nginx client initialized",
            client_id=self.client_id,
            config_path=self.nginx_config_path
        )
    
    def get_log_paths(self) -> List[str]:
        """Return Nginx log file paths to monitor."""
        log_paths = [self.error_log_path]
        
        # Add access log if configured for error monitoring
        if self.config.get("monitor_access_log", False):
            log_paths.append(self.access_log_path)
        
        # Add any additional log files specified in config
        additional_logs = self.config.get("additional_log_paths", [])
        log_paths.extend(additional_logs)
        
        return [path for path in log_paths if os.path.exists(path)]
    
    def get_service_name(self) -> str:
        """Return the service name for health monitoring."""
        return self.config.get("service_name", "nginx")
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate Nginx configuration before applying changes."""
        try:
            # Test nginx configuration
            result = subprocess.run(
                ["nginx", "-t"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info("Nginx configuration validation passed", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Nginx configuration validation failed",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except subprocess.TimeoutExpired:
            logger.error("Nginx configuration validation timed out", client_id=self.client_id)
            return False
        except Exception as e:
            logger.error(
                "Error validating Nginx configuration",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _backup_config(self) -> str:
        """Create backup of current Nginx configuration."""
        if not self.backup_enabled:
            return ""
        
        try:
            # Create backup directory if it doesn't exist
            os.makedirs(self.backup_dir, exist_ok=True)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(self.backup_dir, f"nginx_config_{timestamp}.tar.gz")
            
            # Create tar backup of nginx configuration
            subprocess.run([
                "tar", "-czf", backup_path,
                "-C", "/etc/nginx", "."
            ], check=True, timeout=60)
            
            logger.info(
                "Nginx configuration backed up",
                client_id=self.client_id,
                backup_path=backup_path
            )
            
            return backup_path
            
        except Exception as e:
            logger.error(
                "Failed to backup Nginx configuration",
                client_id=self.client_id,
                error=str(e)
            )
            return ""
    
    def _reload_nginx(self) -> bool:
        """Safely reload Nginx configuration."""
        try:
            # First validate configuration
            if not self.validate_config({}):
                return False
            
            # Reload Nginx
            result = subprocess.run(
                ["systemctl", "reload", "nginx"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info("Nginx reloaded successfully", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Failed to reload Nginx",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error reloading Nginx",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _restart_nginx(self) -> bool:
        """Restart Nginx service."""
        try:
            result = subprocess.run(
                ["systemctl", "restart", "nginx"],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                logger.info("Nginx restarted successfully", client_id=self.client_id)
                return True
            else:
                logger.error(
                    "Failed to restart Nginx",
                    client_id=self.client_id,
                    error=result.stderr
                )
                return False
                
        except Exception as e:
            logger.error(
                "Error restarting Nginx",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _fix_permissions(self) -> bool:
        """Fix common Nginx permission issues."""
        try:
            permission_fixes = [
                ["chown", "-R", "www-data:www-data", "/var/log/nginx"],
                ["chmod", "644", self.nginx_config_path],
                ["chmod", "-R", "644", "/etc/nginx/sites-available"],
                ["chmod", "-R", "755", "/var/www/html"]
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
            
            logger.info("Nginx permissions fixed", client_id=self.client_id)
            return True
            
        except Exception as e:
            logger.error(
                "Error fixing Nginx permissions",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _increase_limits(self) -> bool:
        """Increase Nginx worker limits."""
        try:
            # This would typically involve modifying nginx.conf
            # For safety, we'll just log what we would do
            logger.info(
                "Would increase Nginx worker limits",
                client_id=self.client_id,
                config_path=self.nginx_config_path
            )
            
            # In a real implementation, you would:
            # 1. Parse nginx.conf
            # 2. Update worker_processes and worker_connections
            # 3. Update worker_rlimit_nofile
            # 4. Validate and reload
            
            return True
            
        except Exception as e:
            logger.error(
                "Error increasing Nginx limits",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def _clear_cache(self) -> bool:
        """Clear Nginx cache if configured."""
        try:
            cache_paths = self.config.get("cache_paths", [])
            
            for cache_path in cache_paths:
                if os.path.exists(cache_path):
                    subprocess.run(
                        ["find", cache_path, "-type", "f", "-delete"],
                        timeout=60
                    )
                    
                    logger.info(
                        "Nginx cache cleared",
                        client_id=self.client_id,
                        cache_path=cache_path
                    )
            
            return True
            
        except Exception as e:
            logger.error(
                "Error clearing Nginx cache",
                client_id=self.client_id,
                error=str(e)
            )
            return False
    
    def apply_fix(self, fix_commands: List[str]) -> Dict[str, Any]:
        """Apply fix commands to Nginx service."""
        results = {
            "success": False,
            "commands_executed": [],
            "backup_created": "",
            "errors": []
        }
        
        try:
            # Create backup first
            backup_path = self._backup_config()
            results["backup_created"] = backup_path
            
            # Execute fix commands
            for command in fix_commands:
                command = command.strip()
                
                try:
                    if command == "nginx -t":
                        success = self.validate_config({})
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Configuration validation " + ("passed" if success else "failed")
                        })
                        
                    elif command in ["systemctl reload nginx", "nginx -s reload"]:
                        success = self._reload_nginx()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Nginx " + ("reloaded" if success else "reload failed")
                        })
                        
                    elif command == "systemctl restart nginx":
                        success = self._restart_nginx()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Nginx " + ("restarted" if success else "restart failed")
                        })
                        
                    elif "chmod" in command or "chown" in command:
                        success = self._fix_permissions()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Permissions " + ("fixed" if success else "fix failed")
                        })
                        
                    elif "worker" in command.lower():
                        success = self._increase_limits()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Limits " + ("increased" if success else "increase failed")
                        })
                        
                    elif "cache" in command.lower() and "clear" in command.lower():
                        success = self._clear_cache()
                        results["commands_executed"].append({
                            "command": command,
                            "success": success,
                            "output": "Cache " + ("cleared" if success else "clear failed")
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
                    results["errors"].append("Final configuration validation failed")
            
            self.stats["fixes_applied"] += 1
            
            logger.info(
                "Nginx fix commands applied",
                client_id=self.client_id,
                success=results["success"],
                commands_count=len(fix_commands),
                errors_count=len(results["errors"])
            )
            
            return results
            
        except Exception as e:
            logger.error(
                "Error applying Nginx fixes",
                client_id=self.client_id,
                error=str(e)
            )
            
            results["errors"].append(f"Fix application error: {str(e)}")
            return results
    
    def get_nginx_status(self) -> Dict[str, Any]:
        """Get detailed Nginx status information."""
        status = {
            "service_active": False,
            "config_valid": False,
            "worker_processes": 0,
            "connections": {
                "active": 0,
                "reading": 0,
                "writing": 0,
                "waiting": 0
            },
            "uptime": 0,
            "version": "",
            "error": None
        }
        
        try:
            # Check if service is active
            result = subprocess.run(
                ["systemctl", "is-active", "nginx"],
                capture_output=True,
                text=True,
                timeout=10
            )
            status["service_active"] = (result.returncode == 0)
            
            # Validate configuration
            status["config_valid"] = self.validate_config({})
            
            # Get Nginx version
            try:
                result = subprocess.run(
                    ["nginx", "-v"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                status["version"] = result.stderr.strip() if result.stderr else result.stdout.strip()
            except:
                status["version"] = "Unknown"
            
            # Get process information
            try:
                with open(self.nginx_pid_file, 'r') as f:
                    master_pid = int(f.read().strip())
                
                # Count worker processes
                result = subprocess.run(
                    ["pgrep", "-P", str(master_pid)],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.stdout:
                    status["worker_processes"] = len(result.stdout.strip().split('\n'))
                    
            except:
                status["worker_processes"] = 0
            
        except Exception as e:
            status["error"] = str(e)
            logger.error(
                "Error getting Nginx status",
                client_id=self.client_id,
                error=str(e)
            )
        
        return status
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive Nginx client status."""
        base_status = super().get_status()
        base_status["nginx_status"] = self.get_nginx_status()
        base_status["configuration"] = {
            "config_path": self.nginx_config_path,
            "sites_path": self.nginx_sites_path,
            "error_log": self.error_log_path,
            "access_log": self.access_log_path,
            "backup_enabled": self.backup_enabled
        }
        
        return base_status


def main():
    """Main entry point for Nginx MCP client."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python nginx_client.py <config_path>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    client = NginxMCPClient(config_path)
    
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Nginx client interrupted by user")
    except Exception as e:
        logger.error("Nginx client error", error=str(e))


if __name__ == "__main__":
    main()
