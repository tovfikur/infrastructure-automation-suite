"""
Claude API Client for MCP Distributed Monitoring System.
Handles communication with Anthropic's Claude API with retry logic and fallback strategies.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass
from datetime import datetime
import structlog
import anthropic
from anthropic import Anthropic, AsyncAnthropic
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


class ClaudeAPIError(Exception):
    """Custom exception for Claude API errors."""
    def __init__(self, message: str, error_code: str = None, retry_after: int = None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class ClaudeClient:
    """Production-ready Claude API client with comprehensive error handling."""
    
    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        timeout: int = 300
    ):
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        
        # Initialize both sync and async clients
        self.client = Anthropic(api_key=api_key)
        self.async_client = AsyncAnthropic(api_key=api_key)
        
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
            except anthropic.RateLimitError as e:
                last_exception = e
                if attempt == max_retries:
                    break
                
                # Extract retry-after from headers if available
                retry_after = getattr(e, 'retry_after', None) or (2 ** attempt)
                delay = min(retry_after, self.max_delay)
                
                logger.warning(
                    "Rate limit exceeded, retrying",
                    attempt=attempt + 1,
                    delay=delay,
                    max_retries=max_retries
                )
                await asyncio.sleep(delay)
                
            except anthropic.APIError as e:
                last_exception = e
                if attempt == max_retries:
                    break
                
                delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                logger.warning(
                    "API error, retrying",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(e)
                )
                await asyncio.sleep(delay)
                
            except Exception as e:
                # For unexpected errors, don't retry
                logger.error("Unexpected error in Claude API call", error=str(e))
                raise ClaudeAPIError(f"Unexpected error: {str(e)}")
        
        # All retries exhausted
        raise ClaudeAPIError(
            f"Failed after {max_retries} retries: {str(last_exception)}",
            error_code=getattr(last_exception, 'status_code', None)
        )
    
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
    
    async def _make_api_call(
        self,
        messages: List[Dict[str, str]],
        request_id: str
    ) -> anthropic.types.Message:
        """Make API call to Claude with proper error handling."""
        try:
            response = await self.async_client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.1,
                system=self._build_system_prompt(),
                messages=messages,
                timeout=self.timeout
            )
            
            logger.info(
                "Claude API call successful",
                request_id=request_id,
                model=self.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens
            )
            
            return response
            
        except anthropic.AuthenticationError:
            raise ClaudeAPIError("Invalid API key", "auth_error")
        except anthropic.PermissionDeniedError:
            raise ClaudeAPIError("Permission denied", "permission_error")
        except anthropic.NotFoundError:
            raise ClaudeAPIError("Model not found", "not_found")
        except anthropic.BadRequestError as e:
            raise ClaudeAPIError(f"Bad request: {str(e)}", "bad_request")
        except Exception as e:
            logger.error("Unexpected error in API call", error=str(e))
            raise
    
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
            
            response = await self._exponential_backoff_retry(
                self._make_api_call,
                messages,
                request_id
            )
            
            # Parse response
            parsed_response = self._parse_claude_response(response.content[0].text)
            
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
            
        except ClaudeAPIError as e:
            logger.error(
                "Claude API error, using fallback",
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
