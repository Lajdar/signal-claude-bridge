# Signal-Claude Bridge

A lightweight daemon that connects Signal Messenger to Claude Code AI agents via signal-cli's JSON-RPC interface. Runs on a Raspberry Pi with zero external Python dependencies.

## Architecture

```
Signal App                signal-cli daemon            this bridge              Claude Code CLI
    │                          │                           │                         │
    │──── encrypted msg ──────>│                           │                         │
    │                          │── JSON-RPC notification ─>│                         │
    │                          │                           │── claude -p --agent ───>│
    │                          │                           │                         │
    │                          │                           │<── stdout response ─────│
    │                          │<── JSON-RPC send ─────────│                         │
    │<─── encrypted msg ───────│                           │                         │
```

## Features

- **Zero dependencies** — stdlib-only Python, no pip install needed
- **Single file** — the entire bridge is one ~400-line Python script
- **Whitelist access control** — only configured phone numbers get responses
- **Input sanitization** — control characters stripped, message length enforced
- **Rate limiting** — per-sender cooldown prevents abuse
- **Progress notifications** — periodic "Still working..." messages during long runs
- **Markdown stripping** — converts Claude's markdown to clean plaintext for Signal
- **Exponential backoff** — reconnects gracefully when signal-cli restarts
- **Clean shutdown** — SIGTERM/SIGINT handled; in-flight requests complete before exit
- **Runs on Raspberry Pi** — tested on ARM64, minimal memory footprint (~13 MB)

## Prerequisites

- **Python 3.10+**
- **[signal-cli](https://github.com/AsamK/signal-cli) 0.13+** with JSON-RPC TCP daemon mode
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** (`claude` binary in PATH)
- A registered Signal account linked to signal-cli

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/signal-claude-bridge.git
   cd signal-claude-bridge
   ```

2. **Create your config file:**
   ```bash
   cp config.example.json ~/.config/system-config.json
   # Edit the signal_claude_bridge section with your phone number and whitelist
   ```

   Or add the `signal_claude_bridge` section to an existing `~/.config/system-config.json`.

3. **Install systemd services:**
   ```bash
   sudo cp systemd/signal-cli-daemon.service /etc/systemd/system/
   sudo cp systemd/signal-claude-bridge.service /etc/systemd/system/
   # Edit both files to set your account number and installation path
   sudo systemctl daemon-reload
   ```

4. **Start the services:**
   ```bash
   sudo systemctl enable --now signal-cli-daemon
   sudo systemctl enable --now signal-claude-bridge
   ```

5. **Verify:**
   ```bash
   journalctl -u signal-claude-bridge -f
   # Should see: "Connected to signal-cli at 127.0.0.1:7583"
   ```

## Configuration

All settings live under the `signal_claude_bridge` key in `~/.config/system-config.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `account_number` | *(required)* | Your Signal phone number (e.g. `+1234567890`) |
| `whitelisted_numbers` | *(required)* | List of phone numbers allowed to send messages |
| `signal_cli_socket_host` | `127.0.0.1` | signal-cli daemon TCP host |
| `signal_cli_socket_port` | `7583` | signal-cli daemon TCP port |
| `agent` | `network-stremio-fixer` | Claude Code agent name to invoke |
| `claude_timeout_seconds` | `120` | Max time to wait for Claude response |
| `max_message_length` | `4000` | Truncate outgoing messages beyond this length |
| `cooldown_seconds` | `30` | Per-sender rate limit cooldown |
| `max_input_length` | `1000` | Max characters accepted from incoming messages |
| `progress_interval_seconds` | `30` | How often to send "Still working..." updates |

## How It Works

1. The bridge connects to signal-cli's TCP JSON-RPC daemon and listens for `receive` notifications
2. When a message arrives from a whitelisted sender, it's sanitized and rate-checked
3. The message is passed to Claude Code CLI as a subprocess: `claude -p --agent <name> --permission-mode auto`
4. While Claude is working, periodic progress updates are sent back to the user
5. Claude's response is stripped of markdown formatting and sent back via Signal
6. Only one Claude invocation runs at a time (semaphore) to avoid overloading the host

The bridge handles both direct messages and "Note to Self" sync messages, so you can message yourself from your primary device.

## Security Considerations

- **Whitelist-only access** — the bridge refuses to process messages from numbers not in the whitelist and won't start with an empty whitelist
- **Input sanitization** — control characters are stripped, message length is capped
- **No shell execution** — Claude is invoked via `subprocess.Popen` with an argv list, not through a shell
- **Rate limiting** — prevents any single sender from flooding the system
- **Log redaction** — phone numbers are redacted in log output
- **Error sanitization** — internal errors are not exposed to Signal users
- **Recommendation:** run the bridge as a non-root user and restrict `~/.config/system-config.json` to mode `0600`

## License

[MIT](LICENSE)
