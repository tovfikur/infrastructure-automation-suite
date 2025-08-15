"""
Claude Desktop Client for MCP Distributed Monitoring System.
Handles communication with Claude Desktop app directly instead of API calls.
"""

import asyncio
import json
import time
import subprocess
import tempfile
import os
import uuid
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass
from datetime import datetime
import structlog
from .token_manager import TokenUsage

logger = structlog.get_logger(__name__)


@dataclass
class FixResponse:
    """Represents a response from Claude with fix suggestions."""
    fix_commands: List[str]
    explanation: str
    confidence: float
    requires_restart: bool
    rollback_commands: List[str]
    validation_commands: List[str]
    estimated_downtime: int  # seconds
    risk_level: str  # low, medium, high
    success: bool


@dataclass
class BatchFixResponse:
    """Response for batched fix requests."""
    fixes: List[FixResponse]
    batch_id: str
    processing_time: float
    total_tokens_used: int


class ClaudeDesktopError(Exception):
    """Custom exception for Claude Desktop integration errors."""
    def __init__(self, message: str, error_code: str = None, retry_after: int = None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class ClaudeClient:
    """Production-ready Claude Desktop client with comprehensive error handling."""
    
    def __init__(
        self,
        claude_desktop_path: str = None,
        model: str = "claude-3-5-sonnet-20241022",
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        timeout: int = 300
    ):
        self.claude_desktop_path = claude_desktop_path or self._find_claude_desktop()
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        
        # Working directory for temporary files
        self.temp_dir = tempfile.mkdtemp(prefix="mcp_claude_")
        
        # Check if Claude Desktop is available
        if not self._is_claude_desktop_available():
            logger.warning("Claude Desktop not found, falling back to offline mode")
        
    def _find_claude_desktop(self) -> Optional[str]:
        """Find Claude Desktop installation path."""
        possible_paths = [
            # Windows paths
            r"C:\Users\{}\AppData\Local\Anthropic\Claude\Claude.exe".format(os.getenv('USERNAME', '')),
            r"C:\Program Files\Anthropic\Claude\Claude.exe",
            r"C:\Program Files (x86)\Anthropic\Claude\Claude.exe",
            # macOS paths
            "/Applications/Claude.app/Contents/MacOS/Claude",
            # Linux paths
            "/usr/bin/claude",
            "/usr/local/bin/claude",
            "/opt/claude/claude",
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                logger.info(f"Found Claude Desktop at: {path}")
                return path
        
        logger.warning("Claude Desktop not found in standard locations")
        return None
    
    def _is_claude_desktop_available(self) -> bool:
        """Check if Claude Desktop is available and running."""
        if not self.claude_desktop_path or not os.path.exists(self.claude_desktop_path):
            return False
        
        try:
            # Try to check if Claude Desktop is responsive
            # This is a placeholder - actual implementation would depend on Claude Desktop's API
            result = subprocess.run(
                [self.claude_desktop_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
            return False
    
    def _create_claude_request_file(self, prompt: str, system_prompt: str = "") -> str:
        """Create a temporary file with the request for Claude Desktop."""
        request_id = str(uuid.uuid4())
        request_file = os.path.join(self.temp_dir, f"request_{request_id}.txt")
        
        # Create formatted request
        content = f"""=== SYSTEM PROMPT ===
{system_prompt}

=== USER REQUEST ===
{prompt}

=== INSTRUCTIONS ===
Please provide a JSON response with the following structure:
{{
  "fix_commands": ["command1", "command2"],
  "explanation": "Clear explanation of the fix",
  "confidence": 0.95,
  "requires_restart": true,
  "rollback_commands": ["rollback1", "rollback2"],
  "validation_commands": ["test1", "test2"],
  "estimated_downtime": 30,
  "risk_level": "medium"
}}
"""
        
        with open(request_file, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return request_file
    
    def _get_claude_response_via_desktop(self, request_file: str) -> Optional[str]:
        """Get response from Claude Desktop app."""
        try:
            response_file = request_file.replace('request_', 'response_')
            
            # This is a conceptual implementation
            # In practice, you might need to use different methods like:
            # 1. Automation tools (pyautogui, selenium)
            # 2. Clipboard integration
            # 3. File watching system
            # 4. IPC if Claude Desktop supports it
            
            # For now, we'll simulate the interaction
            # You would need to implement the actual Claude Desktop automation here
            response = self._simulate_claude_desktop_interaction(request_file)
            
            if response:
                with open(response_file, 'w', encoding='utf-8') as f:
                    f.write(response)
                return response
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting response from Claude Desktop: {str(e)}")
            return None
    
    def _simulate_claude_desktop_interaction(self, request_file: str) -> Optional[str]:
        """Interact with Claude Desktop using automation.
        
        This implementation uses GUI automation to:
        1. Open Claude Desktop
        2. Copy the request content
        3. Paste it into Claude
        4. Wait for response
        5. Copy the response
        6. Return the result
        """
        
        try:
            from .claude_desktop_automation import ClaudeDesktopAutomation, ClaudeDesktopFileInterface
            
            # Try GUI automation first
            automation = ClaudeDesktopAutomation(self.claude_desktop_path)
            
            if automation.is_available():
                # Read the request content
                with open(request_file, 'r', encoding='utf-8') as f:
                    request_content = f.read()
                
                # Get system prompt and user content
                lines = request_content.split('\n')
                system_prompt = ""
                user_content = ""
                
                current_section = None
                for line in lines:
                    if "=== SYSTEM PROMPT ===" in line:
                        current_section = "system"
                        continue
                    elif "=== USER REQUEST ===" in line:
                        current_section = "user"
                        continue
                    elif "=== INSTRUCTIONS ===" in line:
                        current_section = "instructions"
                        continue
                    elif current_section == "system" and line.strip():
                        system_prompt += line + "\n"
                    elif current_section in ["user", "instructions"] and line.strip():
                        user_content += line + "\n"
                
                # Get response from Claude Desktop
                response = automation.get_response(user_content.strip(), system_prompt.strip())
                
                if response:
                    logger.info("Successfully got response from Claude Desktop via automation")
                    return response
                else:
                    logger.warning("GUI automation failed, trying file interface")
            
            # Fallback to file-based interface
            file_interface = ClaudeDesktopFileInterface()
            
            if file_interface.test_connection():
                with open(request_file, 'r', encoding='utf-8') as f:
                    request_content = f.read()
                
                response = file_interface.get_response(request_content)
                
                if response:
                    logger.info("Successfully got response from Claude Desktop via file interface")
                    return response
            
            logger.info("Claude Desktop interaction methods unavailable - using fallback mode")
            return None
            
        except ImportError:
            logger.warning("Claude Desktop automation dependencies not available - using fallback mode")
            return None
        except Exception as e:
            logger.error(f"Error in Claude Desktop interaction: {str(e)}")
            return None
    
    def _cleanup_temp_files(self, request_file: str):
        """Clean up temporary files."""
        try:
            if os.path.exists(request_file):
                os.remove(request_file)
            response_file = request_file.replace('request_', 'response_')
            if os.path.exists(response_file):
                os.remove(response_file)
        except Exception as e:
            logger.warning(f"Error cleaning up temp files: {str(e)}")

        # Fallback responses for different error types
        self.fallback_responses = {
            "nginx": {
                "config_error": [
                    "nginx -t",  # Test configuration
                    "systemctl reload nginx"  # Reload if valid
                ],
                "permission_error": [
                    "chown -R www-data:www-data /var/log/nginx",
                    "chmod 644 /etc/nginx/nginx.conf"
                ]
            },
            "postgres": {
                "connection_error": [
                    "systemctl status postgresql",
                    "systemctl restart postgresql"
                ],
                "disk_full": [
                    "VACUUM FULL;",
                    "REINDEX DATABASE;"
                ]
            },
            "redis": {
                "memory_error": [
                    "redis-cli CONFIG SET maxmemory-policy allkeys-lru",
                    "redis-cli FLUSHDB"
                ]
            }
        }
    
    async def _exponential_backoff_retry(
        self,
        func,
        *args,
        max_retries: Optional[int] = None,
        **kwargs
    ) -> Any:
        """Execute function with exponential backoff retry logic."""
        max_retries = max_retries or self.max_retries
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except ClaudeDesktopError as e:
                last_exception = e
                if attempt == max_retries:
                    break
                
                delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                logger.warning(
                    "Claude Desktop error, retrying",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(e)
                )
                await asyncio.sleep(delay)
                
            except Exception as e:
                # For unexpected errors, don't retry
                logger.error("Unexpected error in Claude Desktop interaction", error=str(e))
                raise ClaudeDesktopError(f"Unexpected error: {str(e)}")
        
        # All retries exhausted
        raise ClaudeDesktopError(f"Failed after {max_retries} retries: {str(last_exception)}")
    
    def _build_system_prompt(self) -> str:
        """Build the system prompt for error analysis and fixing."""
        return """You are a production systems expert specializing in fixing infrastructure and application errors. 

Your role is to analyze error messages and provide precise, safe fix commands.

CRITICAL REQUIREMENTS:
1. Always provide rollback commands for any changes
2. Include validation commands to verify fixes work
3. Estimate downtime and mark restart requirements clearly
4. Assign risk levels: low, medium, high
5. Only suggest fixes you're confident about (confidence > 0.7)
6. For risky operations, provide step-by-step validation

RESPONSE FORMAT (JSON):
{
  "fix_commands": ["command1", "command2"],
  "explanation": "Clear explanation of the fix",
  "confidence": 0.95,
  "requires_restart": true,
  "rollback_commands": ["rollback1", "rollback2"],
  "validation_commands": ["test1", "test2"],
  "estimated_downtime": 30,
  "risk_level": "medium"
}

SAFETY RULES:
- Never suggest destructive operations without explicit confirmation
- Always test configuration before applying
- Prefer graceful reloads over hard restarts
- Include permission and ownership checks
- Consider service dependencies"""

    def _build_user_prompt(
        self,
        error_messages: List[str],
        context: str,
        service_type: str = "unknown"
    ) -> str:
        """Build user prompt for error analysis."""
        prompt = f"""SYSTEM: {service_type.upper()}
CONTEXT: {context}

ERRORS TO FIX:
"""
        for i, error in enumerate(error_messages, 1):
            prompt += f"{i}. {error}\n"
        
        prompt += """
Please analyze these errors and provide a comprehensive fix plan following the JSON format specified in the system prompt."""
        
        return prompt
    
    async def _make_desktop_call(
        self,
        messages: List[Dict[str, str]],
        request_id: str
    ) -> Optional[str]:
        """Make call to Claude Desktop with proper error handling."""
        try:
            # Combine system prompt and user messages
            system_prompt = self._build_system_prompt()
            user_content = ""
            
            for message in messages:
                if message.get("role") == "user":
                    user_content += message.get("content", "") + "\n"
            
            # Create request file
            request_file = self._create_claude_request_file(user_content, system_prompt)
            
            try:
                # Get response from Claude Desktop
                response_text = self._get_claude_response_via_desktop(request_file)
                
                if response_text:
                    logger.info(
                        "Claude Desktop call successful",
                        request_id=request_id,
                        model=self.model
                    )
                    return response_text
                else:
                    logger.warning(
                        "Claude Desktop call returned no response",
                        request_id=request_id
                    )
                    return None
                    
            finally:
                # Clean up temporary files
                self._cleanup_temp_files(request_file)
            
        except Exception as e:
            logger.error("Unexpected error in Claude Desktop call", error=str(e))
            raise ClaudeDesktopError(f"Desktop interaction error: {str(e)}")
    
    def _parse_claude_response(self, response_text: str) -> Dict[str, Any]:
        """Parse Claude's JSON response with fallback handling."""
        try:
            # Try to find JSON in the response
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                # Fallback: create structured response from text
                return {
                    "fix_commands": [],
                    "explanation": response_text,
                    "confidence": 0.3,
                    "requires_restart": False,
                    "rollback_commands": [],
                    "validation_commands": [],
                    "estimated_downtime": 0,
                    "risk_level": "high"  # Mark as high risk if we can't parse
                }
        except json.JSONDecodeError:
            logger.warning("Failed to parse Claude response as JSON", response=response_text)
            return {
                "fix_commands": [],
                "explanation": "Failed to parse response",
                "confidence": 0.1,
                "requires_restart": False,
                "rollback_commands": [],
                "validation_commands": [],
                "estimated_downtime": 0,
                "risk_level": "high"
            }
    
    def _get_fallback_response(
        self,
        error_message: str,
        service_type: str
    ) -> FixResponse:
        """Get fallback response when API is unavailable."""
        error_lower = error_message.lower()
        
        # Try to match common error patterns
        fallback_fixes = self.fallback_responses.get(service_type, {})
        
        fix_commands = []
        confidence = 0.2
        
        for error_type, commands in fallback_fixes.items():
            if error_type.replace('_', ' ') in error_lower:
                fix_commands = commands
                confidence = 0.5
                break
        
        if not fix_commands:
            # Generic fallback based on service type
            if service_type == "nginx":
                fix_commands = ["nginx -t", "systemctl status nginx"]
            elif service_type == "postgres":
                fix_commands = ["systemctl status postgresql"]
            elif service_type == "redis":
                fix_commands = ["redis-cli ping"]
            else:
                fix_commands = ["systemctl status " + service_type]
        
        return FixResponse(
            fix_commands=fix_commands,
            explanation=f"Fallback fix for {service_type} error (API unavailable)",
            confidence=confidence,
            requires_restart=False,
            rollback_commands=[],
            validation_commands=fix_commands[:1],  # Use first command for validation
            estimated_downtime=0,
            risk_level="medium",
            success=False
        )
    
    async def get_fix_for_error(
        self,
        error_message: str,
        context: str = "",
        service_type: str = "unknown",
        request_id: str = None
    ) -> FixResponse:
        """Get fix suggestion for a single error."""
        request_id = request_id or f"single_{int(time.time() * 1000)}"
        
        try:
            messages = [{
                "role": "user",
                "content": self._build_user_prompt([error_message], context, service_type)
            }]
            
            response_text = await self._exponential_backoff_retry(
                self._make_desktop_call,
                messages,
                request_id
            )
            
            if response_text:
                # Parse response
                parsed_response = self._parse_claude_response(response_text)
                
                fix_response = FixResponse(
                    fix_commands=parsed_response.get("fix_commands", []),
                    explanation=parsed_response.get("explanation", ""),
                    confidence=parsed_response.get("confidence", 0.5),
                    requires_restart=parsed_response.get("requires_restart", False),
                    rollback_commands=parsed_response.get("rollback_commands", []),
                    validation_commands=parsed_response.get("validation_commands", []),
                    estimated_downtime=parsed_response.get("estimated_downtime", 0),
                    risk_level=parsed_response.get("risk_level", "medium"),
                    success=True
                )
                
                return fix_response
            else:
                # No response from Claude Desktop, use fallback
                logger.warning(
                    "No response from Claude Desktop, using fallback",
                    request_id=request_id
                )
                return self._get_fallback_response(error_message, service_type)
            
        except ClaudeDesktopError as e:
            logger.error(
                "Claude Desktop error, using fallback",
                request_id=request_id,
                error=str(e)
            )
            return self._get_fallback_response(error_message, service_type)
        except Exception as e:
            logger.error(
                "Unexpected error getting fix",
                request_id=request_id,
                error=str(e)
            )
            return self._get_fallback_response(error_message, service_type)
    
    async def get_batch_fixes(
        self,
        error_messages: List[str],
        context: str = "",
        service_type: str = "unknown",
        batch_id: str = None
    ) -> BatchFixResponse:
        """Get fixes for multiple errors in a single request."""
        batch_id = batch_id or f"batch_{int(time.time() * 1000)}"
        start_time = time.time()
        
        try:
            messages = [{
                "role": "user",
                "content": self._build_user_prompt(error_messages, context, service_type)
            }]
            
            response = await self._exponential_backoff_retry(
                self._make_api_call,
                messages,
                batch_id
            )
            
            # For batch processing, we expect an array of fixes
            response_text = response.content[0].text
            
            # Try to parse as array of JSON objects
            fixes = []
            try:
                import re
                json_objects = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text)
                for json_obj in json_objects:
                    parsed = json.loads(json_obj)
                    fixes.append(FixResponse(
                        fix_commands=parsed.get("fix_commands", []),
                        explanation=parsed.get("explanation", ""),
                        confidence=parsed.get("confidence", 0.5),
                        requires_restart=parsed.get("requires_restart", False),
                        rollback_commands=parsed.get("rollback_commands", []),
                        validation_commands=parsed.get("validation_commands", []),
                        estimated_downtime=parsed.get("estimated_downtime", 0),
                        risk_level=parsed.get("risk_level", "medium"),
                        success=True
                    ))
            except:
                # Fallback: create one fix for all errors
                parsed_response = self._parse_claude_response(response_text)
                fixes = [FixResponse(
                    fix_commands=parsed_response.get("fix_commands", []),
                    explanation=parsed_response.get("explanation", ""),
                    confidence=parsed_response.get("confidence", 0.5),
                    requires_restart=parsed_response.get("requires_restart", False),
                    rollback_commands=parsed_response.get("rollback_commands", []),
                    validation_commands=parsed_response.get("validation_commands", []),
                    estimated_downtime=parsed_response.get("estimated_downtime", 0),
                    risk_level=parsed_response.get("risk_level", "medium"),
                    success=True
                )]
            
            return BatchFixResponse(
                fixes=fixes,
                batch_id=batch_id,
                processing_time=time.time() - start_time,
                total_tokens_used=response.usage.input_tokens + response.usage.output_tokens
            )
            
        except Exception as e:
            logger.error(
                "Error in batch processing, using fallbacks",
                batch_id=batch_id,
                error=str(e)
            )
            
            # Return fallback fixes for each error
            fallback_fixes = []
            for error in error_messages:
                fallback_fixes.append(self._get_fallback_response(error, service_type))
            
            return BatchFixResponse(
                fixes=fallback_fixes,
                batch_id=batch_id,
                processing_time=time.time() - start_time,
                total_tokens_used=0
            )
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Get client usage statistics."""
        return {
            "model": self.model,
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "timeout": self.timeout
        }
