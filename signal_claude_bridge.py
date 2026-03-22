#!/usr/bin/env python3
"""Signal-to-Claude-Code bridge daemon.

Connects to signal-cli's JSON-RPC daemon via TCP socket,
listens for incoming messages from whitelisted numbers,
dispatches them to Claude Code's network-stremio-fixer agent,
and sends responses back via Signal.
"""

import json
import logging
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "system-config.json"

DEFAULT_CONFIG = {
    "signal_cli_socket_host": "127.0.0.1",
    "signal_cli_socket_port": 7583,
    "account_number": "",
    "whitelisted_numbers": [],
    "claude_timeout_seconds": 120,
    "max_message_length": 4000,
    "cooldown_seconds": 30,
    "agent": "network-stremio-fixer",
    "max_input_length": 1000,
    "progress_interval_seconds": 30,
}

logger = logging.getLogger("signal-claude-bridge")

# Serialize Claude invocations so only one runs at a time on the Pi
claude_semaphore = threading.Semaphore(1)


def load_config() -> dict:
    """Load bridge config from system-config.json under 'signal_claude_bridge' key."""
    with open(CONFIG_PATH) as f:
        full_config = json.load(f)
    return {**DEFAULT_CONFIG, **full_config.get("signal_claude_bridge", {})}


MAX_BUFFER_SIZE = 1 * 1024 * 1024  # 1 MB — prevent unbounded memory growth


class SignalRpcClient:
    """JSON-RPC client for signal-cli TCP daemon."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._id = 0
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((self.host, self.port))
        return sock

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request on a fresh connection and return the response."""
        sock = self._connect()
        try:
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._next_id(),
            }
            sock.sendall((json.dumps(request) + "\n").encode())

            # Read response (newline-delimited)
            sock.settimeout(30)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("signal-cli socket closed")
                buf += chunk
                if len(buf) > MAX_BUFFER_SIZE:
                    raise ConnectionError("RPC response exceeded buffer limit")

            return json.loads(buf.split(b"\n", 1)[0].decode())
        finally:
            sock.close()


class SignalClaudeBridge:
    def __init__(self, config: dict):
        self.config = config
        self.rpc = SignalRpcClient(
            config["signal_cli_socket_host"],
            config["signal_cli_socket_port"],
        )
        self.last_request_time: dict[str, float] = {}
        self.rate_lock = threading.Lock()
        self.running = True

    @staticmethod
    def strip_markdown(text: str) -> str:
        """Convert markdown to plain text for Signal."""
        # Headers: ## Heading -> HEADING
        text = re.sub(r"^#{1,6}\s+(.+)$", lambda m: m.group(1).upper(), text, flags=re.MULTILINE)
        # Bold: **text** or __text__
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        # Italic: *text* or _text_
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
        # Strikethrough: ~~text~~
        text = re.sub(r"~~(.+?)~~", r"\1", text)
        # Inline code: `code`
        text = re.sub(r"`([^`]+)`", r"\1", text)
        # Code blocks: ```lang\ncode\n```
        text = re.sub(r"```\w*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
        # Links: [text](url) -> text (url)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        # Images: ![alt](url) -> [Image: alt]
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"[Image: \1]", text)
        # Bullet lists: - item or * item -> • item
        text = re.sub(r"^[\s]*[-*]\s+", "• ", text, flags=re.MULTILINE)
        # Horizontal rules
        text = re.sub(r"^[-*_]{3,}\s*$", "---", text, flags=re.MULTILINE)
        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def send_message(self, recipient: str, message: str):
        """Send a Signal message, stripping markdown and truncating if needed."""
        message = self.strip_markdown(message)
        max_len = self.config["max_message_length"]
        if len(message) > max_len:
            message = message[: max_len - 20] + "\n\n[truncated]"

        try:
            self.rpc.call("send", {
                "recipient": [recipient],
                "message": message,
                "account": self.config["account_number"],
            })
        except Exception as e:
            logger.error("Failed to send message to %s: %s", recipient, e)

    @staticmethod
    def sanitize_input(message: str, max_length: int) -> str:
        """Sanitize user input: strip control characters and enforce length limit."""
        # Remove control characters except newlines and tabs
        message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", message)
        if len(message) > max_length:
            message = message[:max_length]
        return message.strip()

    def invoke_claude(self, user_message: str, sender: str | None = None) -> str:
        """Run claude -p with the configured agent and return output.

        If sender is provided, sends periodic progress messages while waiting.
        """
        agent = self.config["agent"]
        timeout = self.config["claude_timeout_seconds"]
        max_input = self.config.get("max_input_length", 1000)
        interval = self.config.get("progress_interval_seconds", 30)

        user_message = self.sanitize_input(user_message, max_input)
        if not user_message:
            return "Empty message received."

        cmd = [
            "claude", "-p",
            "--agent", agent,
            "--permission-mode", "auto",
            "--no-session-persistence",
            user_message,
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd="/root",
            )

            start_time = time.time()
            deadline = start_time + timeout
            last_update = start_time

            while proc.poll() is None:
                now = time.time()
                if now > deadline:
                    proc.kill()
                    proc.wait()
                    return f"Timed out after {timeout}s. Try again or check manually."

                if sender and (now - last_update) >= interval:
                    elapsed = int(now - start_time)
                    self.send_message(sender, f"Still working... ({elapsed}s elapsed)")
                    last_update = now

                time.sleep(1)

            output = proc.stdout.read().strip()
            if proc.returncode != 0 and not output:
                stderr_snippet = proc.stderr.read().strip()[:200]
                logger.warning("Claude exited %d: %s", proc.returncode, stderr_snippet)
                output = "Something went wrong processing your request. Try again shortly."
            return output or "No output from Claude."
        except Exception as e:
            logger.error("Failed to invoke Claude: %s", e)
            return "Failed to process your request. The system may need attention."

    def is_rate_limited(self, sender: str) -> bool:
        """Check if sender is within cooldown period."""
        now = time.time()
        cooldown = self.config["cooldown_seconds"]
        with self.rate_lock:
            last = self.last_request_time.get(sender, 0)
            if now - last < cooldown:
                return True
            self.last_request_time[sender] = now
            return False

    @staticmethod
    def _redact_number(number: str) -> str:
        """Redact phone number for logging: +6148XXXX092 style."""
        if len(number) > 6:
            return number[:4] + "X" * (len(number) - 7) + number[-3:]
        return "REDACTED"

    def handle_message(self, sender: str, message: str):
        """Process an incoming message and send response."""
        whitelist = self.config["whitelisted_numbers"]
        if whitelist and sender not in whitelist:
            logger.info("Ignoring message from non-whitelisted sender")
            return

        if self.is_rate_limited(sender):
            self.send_message(sender, "Please wait before sending another request.")
            return

        redacted = self._redact_number(sender)
        logger.info("Processing from %s (%d chars)", redacted, len(message))
        self.send_message(sender, "Received. Running diagnostics...")

        # Serialize Claude runs to avoid overloading the Pi
        with claude_semaphore:
            response = self.invoke_claude(message, sender=sender)

        self.send_message(sender, response)
        logger.info("Sent response to %s (%d chars)", redacted, len(response))

    def listen(self):
        """Main loop: connect to signal-cli TCP daemon and listen for messages."""
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((
                    self.config["signal_cli_socket_host"],
                    self.config["signal_cli_socket_port"],
                ))
                logger.info(
                    "Connected to signal-cli at %s:%d",
                    self.config["signal_cli_socket_host"],
                    self.config["signal_cli_socket_port"],
                )

                buf = b""
                while self.running:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("Socket closed")
                    buf += chunk
                    if len(buf) > MAX_BUFFER_SIZE:
                        logger.warning("Listener buffer exceeded limit, resetting")
                        buf = b""
                        continue

                    # Process complete JSON lines
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue

                        try:
                            msg = json.loads(line.decode())
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue

                        # JSON-RPC notification for received message
                        if msg.get("method") == "receive":
                            envelope = msg.get("params", {}).get("envelope", {})
                            sender = envelope.get("sourceNumber", "")
                            body = ""

                            # Case 1: Direct message from someone else
                            data_message = envelope.get("dataMessage")
                            if data_message:
                                body = data_message.get("message", "")

                            # Case 2: Sync message (Note to Self / sent from primary device)
                            sync_message = envelope.get("syncMessage", {}).get("sentMessage")
                            if sync_message and not body:
                                body = sync_message.get("message", "")
                                # For Note to Self, destination == source
                                dest = sync_message.get("destinationNumber", "")
                                if dest and dest != sender:
                                    # This is a sync of a message sent to someone else, skip
                                    body = ""

                            if sender and body:
                                threading.Thread(
                                    target=self.handle_message,
                                    args=(sender, body),
                                    daemon=True,
                                ).start()

            except (ConnectionError, OSError) as e:
                logger.warning("Connection error: %s. Reconnecting in 10s...", e)
            except Exception as e:
                logger.exception("Unexpected error: %s", e)
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if self.running:
                time.sleep(10)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config()

    if not config.get("account_number"):
        logger.error("account_number not set in system-config.json signal_claude_bridge section")
        sys.exit(1)

    if not config.get("whitelisted_numbers"):
        logger.error("whitelisted_numbers is empty — refusing to start (security)")
        sys.exit(1)

    bridge = SignalClaudeBridge(config)

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        bridge.running = False
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("Signal-Claude bridge starting (agent: %s)", config["agent"])
    bridge.listen()


if __name__ == "__main__":
    main()
