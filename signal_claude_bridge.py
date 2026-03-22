#!/usr/bin/env python3
"""Signal-to-Claude-Code bridge daemon.

Connects to signal-cli's JSON-RPC daemon via TCP socket,
listens for incoming messages from whitelisted numbers,
dispatches them to a Claude Code agent,
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_BUFFER_SIZE = 1_048_576  # 1 MB — prevent unbounded memory growth
RECV_CHUNK_SIZE = 4096
CONNECT_TIMEOUT = 10  # seconds
RPC_TIMEOUT = 30  # seconds
LISTEN_SOCKET_TIMEOUT = 5  # seconds
MIN_BACKOFF = 5  # seconds
MAX_BACKOFF = 300  # seconds (5 min)
RATE_LIMIT_CLEANUP_THRESHOLD = 100


def load_config() -> dict:
    """Load bridge config from system-config.json under 'signal_claude_bridge' key."""
    with open(CONFIG_PATH) as f:
        full_config = json.load(f)
    return {**DEFAULT_CONFIG, **full_config.get("signal_claude_bridge", {})}


# ---------------------------------------------------------------------------
# JSON-RPC Client
# ---------------------------------------------------------------------------


class SignalRpcClient:
    """JSON-RPC client for signal-cli TCP daemon."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._id = 0
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
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

            sock.settimeout(RPC_TIMEOUT)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(RECV_CHUNK_SIZE)
                if not chunk:
                    raise ConnectionError("signal-cli socket closed")
                buf += chunk
                if len(buf) > MAX_BUFFER_SIZE:
                    raise ConnectionError("RPC response exceeded buffer limit")

            return json.loads(buf.split(b"\n", 1)[0].decode())
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class SignalClaudeBridge:
    """Bridge between Signal Messenger and Claude Code AI agents.

    Listens for incoming Signal messages from whitelisted senders,
    invokes a Claude Code agent, and returns the response via Signal.
    Handles rate limiting, input sanitization, and graceful shutdown.
    """

    def __init__(self, config: dict):
        self.config = config
        self.rpc = SignalRpcClient(
            config["signal_cli_socket_host"],
            config["signal_cli_socket_port"],
        )
        self.last_request_time: dict[str, float] = {}
        self.rate_lock = threading.Lock()
        self.running = threading.Event()
        self.running.set()
        self._claude_semaphore = threading.Semaphore(1)
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()

    # -- Formatting ----------------------------------------------------------

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

    # -- Messaging -----------------------------------------------------------

    def send_message(self, recipient: str, message: str) -> None:
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
            logger.error(f"Failed to send message to {self._redact_number(recipient)}: {e}")

    # -- Input handling ------------------------------------------------------

    @staticmethod
    def sanitize_input(message: str, max_length: int) -> str:
        """Sanitize user input: strip control characters and enforce length limit."""
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
                cwd=str(Path.home()),
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
                logger.warning(f"Claude exited {proc.returncode}: {stderr_snippet}")
                output = "Something went wrong processing your request. Try again shortly."
            return output or "No output from Claude."
        except Exception as e:
            logger.error(f"Failed to invoke Claude: {e}")
            return "Failed to process your request. The system may need attention."

    # -- Rate limiting -------------------------------------------------------

    def is_rate_limited(self, sender: str) -> bool:
        """Check if sender is within cooldown period."""
        now = time.time()
        cooldown = self.config["cooldown_seconds"]
        with self.rate_lock:
            last = self.last_request_time.get(sender, 0)
            if now - last < cooldown:
                return True
            self.last_request_time[sender] = now
            # Prevent unbounded growth: prune expired entries periodically
            if len(self.last_request_time) > RATE_LIMIT_CLEANUP_THRESHOLD:
                cutoff = now - cooldown
                expired = [k for k, v in self.last_request_time.items() if v < cutoff]
                for k in expired:
                    del self.last_request_time[k]
            return False

    # -- Logging helpers -----------------------------------------------------

    @staticmethod
    def _redact_number(number: str) -> str:
        """Redact phone number for logging: +6148XXXX092 style."""
        if len(number) > 6:
            return number[:4] + "X" * (len(number) - 7) + number[-3:]
        return "REDACTED"

    # -- Message handling ----------------------------------------------------

    def handle_message(self, sender: str, message: str) -> None:
        """Process an incoming message and send response."""
        whitelist = self.config["whitelisted_numbers"]
        if whitelist and sender not in whitelist:
            logger.info("Ignoring message from non-whitelisted sender")
            return

        if self.is_rate_limited(sender):
            self.send_message(sender, "Please wait before sending another request.")
            return

        redacted = self._redact_number(sender)
        logger.info(f"Processing from {redacted} ({len(message)} chars)")
        self.send_message(sender, "Received. Running diagnostics...")

        with self._claude_semaphore:
            response = self.invoke_claude(message, sender=sender)

        self.send_message(sender, response)
        logger.info(f"Sent response to {redacted} ({len(response)} chars)")

    # -- Notification parsing ------------------------------------------------

    def _process_notification(self, msg: dict) -> None:
        """Parse a JSON-RPC notification and dispatch valid messages."""
        if msg.get("method") != "receive":
            return

        envelope = msg.get("params", {}).get("envelope", {})
        sender = envelope.get("sourceNumber", "")
        if not isinstance(sender, str) or not sender.startswith("+"):
            return

        body = ""

        # Case 1: Direct message from someone else
        data_message = envelope.get("dataMessage")
        if data_message:
            body = data_message.get("message", "")

        # Case 2: Sync message (Note to Self / sent from primary device)
        sync_message = envelope.get("syncMessage", {}).get("sentMessage")
        if sync_message and not body:
            body = sync_message.get("message", "")
            dest = sync_message.get("destinationNumber", "")
            if dest and dest != sender:
                body = ""

        if sender and body:
            t = threading.Thread(
                target=self.handle_message,
                args=(sender, body),
                daemon=True,
            )
            with self._threads_lock:
                self._threads = [t for t in self._threads if t.is_alive()]
                self._threads.append(t)
            t.start()

    # -- Main listener loop --------------------------------------------------

    def listen(self) -> None:
        """Connect to signal-cli TCP daemon and listen for messages."""
        backoff = MIN_BACKOFF

        while self.running.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(LISTEN_SOCKET_TIMEOUT)
                sock.connect((
                    self.config["signal_cli_socket_host"],
                    self.config["signal_cli_socket_port"],
                ))
                logger.info(
                    f"Connected to signal-cli at "
                    f"{self.config['signal_cli_socket_host']}:{self.config['signal_cli_socket_port']}"
                )
                backoff = MIN_BACKOFF  # reset on successful connection

                buf = b""
                while self.running.is_set():
                    try:
                        chunk = sock.recv(RECV_CHUNK_SIZE)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("Socket closed")
                    buf += chunk
                    if len(buf) > MAX_BUFFER_SIZE:
                        raise ConnectionError("Listener buffer exceeded limit")

                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line.decode())
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        self._process_notification(msg)

            except (ConnectionError, OSError) as e:
                logger.warning(f"Connection error: {e}. Reconnecting in {backoff}s...")
            except Exception:
                logger.exception("Unexpected error in listener")
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if self.running.is_set():
                self.running.wait(timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self, timeout: float = 15.0) -> None:
        """Stop listening and wait for in-flight message handlers to finish."""
        self.running.clear()
        with self._threads_lock:
            threads = list(self._threads)
        deadline = time.time() + timeout
        for t in threads:
            remaining = deadline - time.time()
            if remaining > 0:
                t.join(timeout=remaining)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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

    def on_signal(signum, frame):
        logger.info("Shutting down...")
        bridge.shutdown()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    logger.info(f"Signal-Claude bridge starting (agent: {config['agent']})")
    bridge.listen()
    logger.info("Bridge stopped.")


if __name__ == "__main__":
    main()
