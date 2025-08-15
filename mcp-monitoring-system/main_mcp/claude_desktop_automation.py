"""
Claude Desktop Automation Module
Provides automated interaction with Claude Desktop application using GUI automation.
"""

import time
import os
import subprocess
import tempfile
import threading
from typing import Optional, Dict, Any
import structlog

try:
    import pyautogui
    import pyperclip
    GUI_AUTOMATION_AVAILABLE = True
except ImportError:
    GUI_AUTOMATION_AVAILABLE = False

logger = structlog.get_logger(__name__)


class ClaudeDesktopAutomation:
    """Handles automated interaction with Claude Desktop application."""
    
    def __init__(self, claude_path: str = None, response_timeout: int = 60):
        self.claude_path = claude_path
        self.response_timeout = response_timeout
        self.last_response = None
        self.waiting_for_response = False
        
        # GUI automation settings
        if GUI_AUTOMATION_AVAILABLE:
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.5
        
        logger.info("Claude Desktop automation initialized", 
                   automation_available=GUI_AUTOMATION_AVAILABLE)
    
    def is_available(self) -> bool:
        """Check if Claude Desktop automation is available."""
        return GUI_AUTOMATION_AVAILABLE and self._find_claude_window()
    
    def _find_claude_window(self) -> bool:
        """Find Claude Desktop window."""
        if not GUI_AUTOMATION_AVAILABLE:
            return False
        
        try:
            # Try to find Claude window by title
            claude_windows = pyautogui.getWindowsWithTitle("Claude")
            return len(claude_windows) > 0
        except Exception as e:
            logger.debug(f"Error finding Claude window: {str(e)}")
            return False
    
    def _ensure_claude_running(self) -> bool:
        """Ensure Claude Desktop is running."""
        if self._find_claude_window():
            return True
        
        if not self.claude_path or not os.path.exists(self.claude_path):
            logger.error("Claude Desktop path not found")
            return False
        
        try:
            # Start Claude Desktop
            subprocess.Popen([self.claude_path], shell=True)
            
            # Wait for Claude to start
            for _ in range(30):  # Wait up to 30 seconds
                time.sleep(1)
                if self._find_claude_window():
                    logger.info("Claude Desktop started successfully")
                    return True
            
            logger.error("Claude Desktop failed to start within timeout")
            return False
            
        except Exception as e:
            logger.error(f"Error starting Claude Desktop: {str(e)}")
            return False
    
    def _focus_claude_window(self) -> bool:
        """Focus on Claude Desktop window."""
        try:
            claude_windows = pyautogui.getWindowsWithTitle("Claude")
            if claude_windows:
                claude_window = claude_windows[0]
                claude_window.activate()
                time.sleep(1)  # Give window time to focus
                return True
            return False
        except Exception as e:
            logger.error(f"Error focusing Claude window: {str(e)}")
            return False
    
    def _send_message_to_claude(self, message: str) -> bool:
        """Send message to Claude Desktop using GUI automation."""
        try:
            if not self._focus_claude_window():
                return False
            
            # Clear clipboard and set new content
            pyperclip.copy(message)
            time.sleep(0.5)
            
            # Click in the message input area (this might need adjustment based on Claude's UI)
            # You may need to adjust these coordinates based on your screen resolution
            # and Claude Desktop's interface
            
            # Try to find the input field by clicking at the bottom of the window
            screen_width, screen_height = pyautogui.size()
            
            # Click in the lower portion where the input field typically is
            pyautogui.click(screen_width // 2, screen_height - 100)
            time.sleep(0.5)
            
            # Select all existing text and replace with our message
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.2)
            
            # Paste our message
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.5)
            
            # Send the message (typically Enter or Shift+Enter)
            pyautogui.press('enter')
            
            logger.info("Message sent to Claude Desktop")
            return True
            
        except Exception as e:
            logger.error(f"Error sending message to Claude: {str(e)}")
            return False
    
    def _wait_for_response(self) -> Optional[str]:
        """Wait for Claude's response and extract it."""
        try:
            # Wait for Claude to generate response
            # This is a simplified implementation - you might need more sophisticated
            # detection of when Claude has finished responding
            
            logger.info(f"Waiting for Claude response (timeout: {self.response_timeout}s)")
            
            # Wait for the response to be generated
            time.sleep(10)  # Initial wait for Claude to start responding
            
            # Try to select and copy the response
            # This is a simplified approach - you might need to:
            # 1. Detect when Claude has finished typing
            # 2. Scroll to see the full response
            # 3. Select the response text accurately
            
            # Select all text in the conversation
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.5)
            
            # Copy the selection
            pyautogui.hotkey('ctrl', 'c')
            time.sleep(0.5)
            
            # Get the copied text
            response_text = pyperclip.paste()
            
            if response_text and len(response_text.strip()) > 0:
                logger.info("Response received from Claude Desktop")
                return response_text
            else:
                logger.warning("No response text received")
                return None
                
        except Exception as e:
            logger.error(f"Error waiting for Claude response: {str(e)}")
            return None
    
    def _extract_json_from_response(self, response_text: str) -> Optional[str]:
        """Extract JSON from Claude's response text."""
        try:
            import re
            
            # Look for JSON blocks in the response
            json_pattern = r'\{[\s\S]*?\}'
            json_matches = re.findall(json_pattern, response_text)
            
            if json_matches:
                # Return the last (most complete) JSON block
                return json_matches[-1]
            
            # If no JSON found, return the full response
            return response_text
            
        except Exception as e:
            logger.error(f"Error extracting JSON from response: {str(e)}")
            return response_text
    
    def get_response(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """Get response from Claude Desktop for the given prompt."""
        if not GUI_AUTOMATION_AVAILABLE:
            logger.error("GUI automation not available")
            return None
        
        try:
            # Ensure Claude Desktop is running
            if not self._ensure_claude_running():
                return None
            
            # Prepare the full message
            full_message = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
            
            # Send message to Claude
            if not self._send_message_to_claude(full_message):
                return None
            
            # Wait for and get response
            response = self._wait_for_response()
            
            if response:
                # Extract JSON from response
                json_response = self._extract_json_from_response(response)
                return json_response
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting response from Claude Desktop: {str(e)}")
            return None
    
    def test_connection(self) -> bool:
        """Test connection to Claude Desktop."""
        try:
            if not GUI_AUTOMATION_AVAILABLE:
                return False
            
            if not self._ensure_claude_running():
                return False
            
            # Send a simple test message
            test_response = self.get_response("Hello, this is a test message. Please respond with 'Test successful'.")
            
            return test_response is not None and "test" in test_response.lower()
            
        except Exception as e:
            logger.error(f"Error testing Claude Desktop connection: {str(e)}")
            return False


# Alternative implementation using file-based communication
class ClaudeDesktopFileInterface:
    """Alternative Claude Desktop interface using file watching."""
    
    def __init__(self, watch_directory: str = None):
        self.watch_directory = watch_directory or tempfile.mkdtemp(prefix="claude_desktop_")
        self.input_file = os.path.join(self.watch_directory, "claude_input.txt")
        self.output_file = os.path.join(self.watch_directory, "claude_output.txt")
        
        # Ensure directory exists
        os.makedirs(self.watch_directory, exist_ok=True)
        
        logger.info(f"Claude Desktop file interface initialized at {self.watch_directory}")
    
    def get_response(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """Get response using file-based communication."""
        try:
            # Write input file
            full_message = f"System: {system_prompt}\n\nUser: {prompt}" if system_prompt else f"User: {prompt}"
            
            with open(self.input_file, 'w', encoding='utf-8') as f:
                f.write(full_message)
            
            # Clear any existing output
            if os.path.exists(self.output_file):
                os.remove(self.output_file)
            
            logger.info(f"Request written to {self.input_file}")
            logger.info("Please manually copy the input to Claude Desktop and paste the response to the output file.")
            
            # Wait for output file
            timeout = 300  # 5 minutes
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                if os.path.exists(self.output_file):
                    with open(self.output_file, 'r', encoding='utf-8') as f:
                        response = f.read().strip()
                    
                    if response:
                        logger.info("Response received via file interface")
                        return response
                
                time.sleep(1)
            
            logger.warning("Timeout waiting for response file")
            return None
            
        except Exception as e:
            logger.error(f"Error in file-based Claude Desktop interface: {str(e)}")
            return None
    
    def test_connection(self) -> bool:
        """Test the file interface."""
        return os.path.exists(self.watch_directory) and os.access(self.watch_directory, os.W_OK)
