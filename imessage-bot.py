#!/usr/bin/env python3
"""
iMessage ↔ Claude Code bridge.

Monitors your iMessage self-chat, routes messages to Claude Code CLI,
and sends responses back via AppleScript. Supports text, images, and
session persistence.

Usage:
    cp .env.example .env   # edit with your Apple ID
    python3 imessage-bot.py

Requirements:
    - macOS 13+ with iMessage
    - Claude Code CLI installed (https://docs.anthropic.com/en/docs/claude-code)
    - Full Disk Access granted to /usr/bin/python3

Why self-chat? iMessage stores every text in a local SQLite database.
Texting yourself creates a private thread that never leaves your device.
This script watches that database and bridges it to Claude Code.
"""
from __future__ import annotations

import sqlite3
import subprocess
import struct
import hashlib
import json
import os
import sys
import time
import tempfile
import shutil
import signal
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration (loaded from .env)
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

CHAT_DB = Path.home() / "Library/Messages/chat.db"
STATE_FILE = Path.home() / ".imessage-claude-state.json"
APPLE_ID = os.environ.get("APPLE_ID", "")
PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "")
WORKSPACE = os.environ.get("CLAUDE_WORKSPACE", str(Path.home()))
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))
MAX_RESPONSE_LENGTH = 8000
LOG_DIR = Path(os.environ.get("LOG_DIR", ""))  # optional conversation logging

# Staging dir for attachments — Claude CLI can't read ~/Library/Messages/
# because it doesn't have Full Disk Access, so we copy files to /tmp first
ATTACHMENT_STAGING = Path(tempfile.gettempdir()) / "imessage-bot-attachments"

# Prompt prefix for new sessions (reads CLAUDE.md if you have one)
CONTEXT_PROMPT = """First, silently read CLAUDE.md for context if it exists.
Then respond to: """


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def text_hash(text: str) -> str:
    """Hash first 300 chars — used to detect our own sent messages."""
    return hashlib.md5(text[:300].encode()).hexdigest()


# ---------------------------------------------------------------------------
# Apple typedstream parser
# ---------------------------------------------------------------------------
# macOS stores some iMessage text in an NSAttributedString blob (the
# attributedBody column) instead of the text column. This happens for
# messages sent via AppleScript and some system messages. The blob uses
# Apple's "typedstream" binary format. We parse just enough to extract
# the plain text string.

def extract_text_from_attributed_body(blob: bytes) -> str | None:
    """Extract plain text from NSAttributedString typedstream blob."""
    if not blob:
        return None
    try:
        marker = b"NSString"
        idx = blob.find(marker)
        if idx < 0:
            return None
        pos = idx + len(marker)

        # Find the '+' byte (0x2B) which marks string data
        while pos < len(blob) and blob[pos] != 0x2B:
            pos += 1
        if pos >= len(blob):
            return None
        pos += 1  # skip '+'

        # Read variable-length string size
        length_byte = blob[pos]
        pos += 1

        if length_byte < 0x80:
            str_len = length_byte
        elif length_byte == 0x81:
            str_len = struct.unpack("<H", blob[pos : pos + 2])[0]
            pos += 2
        elif length_byte == 0x82:
            str_len = struct.unpack("<I", blob[pos : pos + 3] + b"\x00")[0]
            pos += 3
        elif length_byte == 0x83:
            str_len = struct.unpack("<I", blob[pos : pos + 4])[0]
            pos += 4
        else:
            return None

        text_bytes = blob[pos : pos + str_len]
        return text_bytes.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def get_message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    """Extract message text — prefers text column, falls back to attributedBody."""
    if text and text.strip():
        return text.strip()
    extracted = extract_text_from_attributed_body(attributed_body)
    if extracted and extracted.strip():
        return extracted.strip()
    return None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_processed_rowid": 0, "session_id": None, "sent_hashes": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Database access (resilient FDA handling)
# ---------------------------------------------------------------------------
# macOS Full Disk Access (FDA) can be lost intermittently — the TCC daemon
# may revoke authorization after long-running processes spawn subprocesses.
# Instead of spinning in a tight retry loop (which burns CPU for hours),
# we use a persistent connection manager with exponential backoff and
# self-recovery.

class FDAError(Exception):
    """Raised when Full Disk Access is denied for chat.db."""
    pass


class ChatDB:
    """Persistent, resilient connection to chat.db.

    - Reuses a single connection (fewer TCC checks = less likely to lose FDA).
    - On FDA loss: fast failure (3 retries, 6s), then exponential backoff.
    - Sends iMessage notification after 2 min of downtime.
    - Self-restarts after 10 min to get a fresh TCC context from launchd.
    """

    def __init__(self):
        self._conn: sqlite3.Connection | None = None
        self._fda_lost_since: float | None = None
        self._notified: bool = False
        self._consecutive_failures: int = 0

    def get_connection(self) -> sqlite3.Connection:
        """Get a working connection. Raises FDAError if access is denied."""
        if self._conn:
            try:
                self._conn.execute("SELECT 1 FROM message LIMIT 1")
                self._on_success()
                return self._conn
            except sqlite3.DatabaseError:
                self._close_quiet()

        for attempt in range(3):
            try:
                conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
                conn.execute("SELECT 1 FROM message LIMIT 1")
                self._conn = conn
                self._on_success()
                return conn
            except sqlite3.DatabaseError as e:
                if "authorization" in str(e).lower() and attempt < 2:
                    time.sleep(2)
                    continue
                if "authorization" in str(e).lower():
                    self._on_failure()
                    raise FDAError("Full Disk Access denied for chat.db")
                raise

        self._on_failure()
        raise FDAError("Full Disk Access denied for chat.db")

    def _on_success(self):
        if self._fda_lost_since:
            elapsed = time.time() - self._fda_lost_since
            log(f"[fda-recovered] Access restored after {int(elapsed)}s")
        self._fda_lost_since = None
        self._notified = False
        self._consecutive_failures = 0

    def _on_failure(self):
        now = time.time()
        if not self._fda_lost_since:
            self._fda_lost_since = now
            log("[fda-lost] Authorization denied for chat.db")
        self._consecutive_failures += 1

        # Notify after 2 minutes (once)
        if not self._notified and now - self._fda_lost_since > 120:
            try:
                send_imessage(
                    "Bot lost Full Disk Access to chat.db — messages aren't being processed. "
                    "Restarting in ~10 min to try a fresh TCC context. "
                    "If it persists: System Settings > Privacy & Security > Full Disk Access, "
                    "toggle Python off then on.",
                )
                self._notified = True
                log("[fda-notify] Sent iMessage notification about FDA loss")
            except Exception:
                pass

        # Self-restart after 10 min — fresh process often fixes TCC issues
        if now - self._fda_lost_since > 600:
            log("[fda-restart] FDA lost for 10+ min. Exiting for launchd restart.")
            os._exit(1)

    def get_backoff_delay(self) -> float:
        """Exponential backoff: 30s, 60s, 120s, 300s (cap)."""
        return min(30 * (2 ** min(self._consecutive_failures - 1, 4)), 300)

    def _close_quiet(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @property
    def is_fda_lost(self) -> bool:
        return self._fda_lost_since is not None


_chatdb = ChatDB()


def open_chatdb() -> sqlite3.Connection:
    """Open chat.db via the persistent connection manager."""
    return _chatdb.get_connection()


# ---------------------------------------------------------------------------
# Self-chat discovery
# ---------------------------------------------------------------------------
# iMessage can create TWO self-chat threads: one for your email and one
# for your phone number. We monitor both and reply to whichever thread
# the message came from.

def get_self_chat_ids() -> tuple[list[int], dict[int, str]]:
    """Find all self-chat ROWIDs. Returns (chat_ids, {chat_id: identifier})."""
    identifiers = [APPLE_ID]
    if PHONE_NUMBER:
        identifiers.append(PHONE_NUMBER)

    conn = open_chatdb()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in identifiers)
    cursor.execute(
        f"SELECT ROWID, chat_identifier FROM chat "
        f"WHERE chat_identifier IN ({placeholders}) AND service_name = 'iMessage'",
        identifiers,
    )
    rows = cursor.fetchall()

    chat_ids = [r[0] for r in rows]
    chat_id_map = {r[0]: r[1] for r in rows}
    for rowid, ident in rows:
        log(f"  Found self-chat {rowid}: {ident}")
    return chat_ids, chat_id_map


def get_current_max_rowid(chat_ids: list[int]) -> int:
    conn = open_chatdb()
    cursor = conn.cursor()
    ph = ",".join("?" for _ in chat_ids)
    cursor.execute(
        f"SELECT MAX(m.ROWID) FROM message m "
        f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        f"WHERE cmj.chat_id IN ({ph})",
        chat_ids,
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else 0


def get_new_messages(chat_ids: list[int], last_rowid: int) -> list:
    conn = open_chatdb()
    cursor = conn.cursor()
    ph = ",".join("?" for _ in chat_ids)
    cursor.execute(
        f"""SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me,
               datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime'),
               m.cache_has_attachments, cmj.chat_id
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id IN ({ph}) AND m.ROWID > ?
            AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL
                 OR m.cache_has_attachments = 1)
            ORDER BY m.ROWID ASC""",
        [*chat_ids, last_rowid],
    )
    messages = cursor.fetchall()
    return messages


# ---------------------------------------------------------------------------
# Attachments (staged to /tmp for Claude CLI access)
# ---------------------------------------------------------------------------

def get_attachments(message_rowid: int) -> list[dict]:
    conn = open_chatdb()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT a.filename, a.mime_type, a.transfer_name, a.total_bytes "
        "FROM attachment a "
        "JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id "
        "WHERE maj.message_id = ?",
        (message_rowid,),
    )
    ATTACHMENT_STAGING.mkdir(parents=True, exist_ok=True)
    attachments = []
    for filename, mime_type, transfer_name, total_bytes in cursor.fetchall():
        if not filename:
            continue
        original = filename.replace("~", str(Path.home()), 1)
        staged = original  # fallback
        try:
            src = Path(original)
            if src.exists():
                dst = ATTACHMENT_STAGING / f"{message_rowid}_{src.name}"
                shutil.copy2(str(src), str(dst))
                staged = str(dst)
                log(f"[attachment] Staged: {src.name}")
            else:
                log(f"[attachment] Not found: {original}")
        except Exception as e:
            log(f"[attachment] Copy failed: {e}")
        attachments.append({
            "path": staged,
            "mime_type": mime_type or "",
            "name": transfer_name or "",
            "size": total_bytes or 0,
        })
    return attachments


def cleanup_staged_attachments(max_age: int = 3600):
    try:
        if not ATTACHMENT_STAGING.exists():
            return
        now = time.time()
        for f in ATTACHMENT_STAGING.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > max_age:
                f.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# iMessage sending (AppleScript)
# ---------------------------------------------------------------------------

def send_imessage(text: str, target: str | None = None) -> bool:
    """Send via AppleScript. Replies to `target` (email or phone)."""
    recipient = target or APPLE_ID
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        tmp_path = f.name
    try:
        script = f'''
        set messageText to read POSIX file "{tmp_path}" as «class utf8»
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to participant "{recipient}" of targetService
            send messageText to targetBuddy
        end tell
        '''
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log(f"AppleScript error: {result.stderr}")
            return False
        return True
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Conversation logging (optional)
# ---------------------------------------------------------------------------

def log_exchange(user_text, attachments, response, timestamp=None):
    if not LOG_DIR:
        return
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    ts = timestamp or now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    log_file = log_dir / f"{date_str}.md"

    parts = [f"## {ts}"]
    if user_text:
        parts.append(f"**You:** {user_text}")
    for att in (attachments or []):
        parts.append(f"**Attachment:** {att['name']} ({att['mime_type']})")
    if response:
        parts.append(f"**Claude:** {response}")
    parts.append("---\n")

    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n\n".join(parts))


# ---------------------------------------------------------------------------
# Claude Code CLI
# ---------------------------------------------------------------------------

def run_claude(message: str, session_id: str = None) -> tuple[str, str]:
    cmd = [
        CLAUDE_PATH, "-p", message,
        "--output-format", "json",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    log(f"Running Claude{' (resuming)' if session_id else ''}...")

    # Critical: unset CLAUDECODE so nested sessions work
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=WORKSPACE, timeout=300, env=env,
        )
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler request.", None

    try:
        data = json.loads(result.stdout)
        return data.get("result", "No response"), data.get("session_id")
    except json.JSONDecodeError:
        err = result.stdout or result.stderr or ""
        if "No conversation found" in err or "session" in err.lower():
            return None, None  # signal to retry without session
        return err[:500] or "Error running Claude", None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def advance_past_sent(chat_ids, state):
    """Wait for our sent message to appear in chat.db, then skip past it."""
    for _ in range(5):
        time.sleep(1)
        new_max = get_current_max_rowid(chat_ids)
        if new_max > state["last_processed_rowid"]:
            state["last_processed_rowid"] = new_max
            break


def main():
    if not APPLE_ID:
        print("Set APPLE_ID in .env (your iMessage email)")
        sys.exit(1)
    if not CHAT_DB.exists():
        print(f"chat.db not found at {CHAT_DB} — check Full Disk Access")
        sys.exit(1)

    log("iMessage Claude Bot starting")

    # Startup retries — FDA may take time to activate after launchd restart
    chat_ids = chat_id_map = None
    for startup_attempt in range(10):
        try:
            chat_ids, chat_id_map = get_self_chat_ids()
            break
        except FDAError:
            wait = min(10 * (startup_attempt + 1), 60)
            log(f"[startup] FDA not ready (attempt {startup_attempt + 1}/10), waiting {wait}s...")
            time.sleep(wait)
    else:
        log("[startup] Could not get FDA after 10 attempts. Exiting.")
        sys.exit(1)

    if not chat_ids:
        print(f"No self-chat found for {APPLE_ID}")
        print("Text yourself on iMessage first to create the thread.")
        sys.exit(1)

    log(f"Monitoring {len(chat_ids)} self-chat(s)")
    log(f"Workspace: {WORKSPACE}")

    state = load_state()
    if state["last_processed_rowid"] == 0:
        state["last_processed_rowid"] = get_current_max_rowid(chat_ids)
        save_state(state)
        log(f"Initialized at ROWID {state['last_processed_rowid']}")

    log("Ready — text yourself on iMessage to talk to Claude.\n")

    def shutdown(sig, frame):
        log("\nShutting down...")
        save_state(state)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    cleanup_counter = 0
    while True:
        try:
            cleanup_counter += 1
            if cleanup_counter % 100 == 0:
                cleanup_staged_attachments()

            for rowid, raw_text, attr_body, is_from_me, ts, has_att, chat_id in \
                    get_new_messages(chat_ids, state["last_processed_rowid"]):

                state["last_processed_rowid"] = rowid
                reply_target = chat_id_map.get(chat_id, APPLE_ID)
                text = get_message_text(raw_text, attr_body)
                attachments = get_attachments(rowid) if has_att else []

                if not text and not attachments:
                    save_state(state)
                    continue

                if text:
                    text = text.replace("\ufffc", "").strip()

                # Skip our own sent messages
                if text and text_hash(text) in state.get("sent_hashes", []):
                    save_state(state)
                    continue

                # Commands
                if text and text.strip().lower() == "/new":
                    state["session_id"] = None
                    send_imessage("Session cleared.", reply_target)
                    advance_past_sent(chat_ids, state)
                    save_state(state)
                    continue

                if text and text.strip().lower() == "/status":
                    msg = f"Session: {'active' if state.get('session_id') else 'none'}"
                    send_imessage(msg, reply_target)
                    advance_past_sent(chat_ids, state)
                    save_state(state)
                    continue

                # Build prompt
                log(f"[message] {(text or '[attachment]')[:80]}")
                prompt = text or ""

                if attachments:
                    images = [a for a in attachments if a["mime_type"].startswith("image/")]
                    others = [a for a in attachments if not a["mime_type"].startswith("image/")]
                    if images:
                        paths = [a["path"] for a in images]
                        img_note = (
                            f"The user sent {'an image' if len(paths) == 1 else f'{len(paths)} images'}. "
                            f"Read {'the image at: ' + paths[0] if len(paths) == 1 else 'the images at: ' + ', '.join(paths)} "
                            f"using the Read tool to see what they sent."
                        )
                        prompt = f"{img_note}\n{('Their message: ' + prompt) if prompt else 'Describe or respond to what you see.'}"
                        log(f"[attachment] {len(images)} image(s)")
                    for att in others:
                        prompt += f"\n[Attachment: {att['name']} ({att['mime_type']})]"

                # Run Claude
                session_id = state.get("session_id")
                response, new_sid = (None, None)

                if session_id:
                    response, new_sid = run_claude(prompt, session_id)
                    if response is None:
                        log("Session expired, starting fresh")
                        session_id = None

                if not session_id:
                    response, new_sid = run_claude(CONTEXT_PROMPT + prompt)

                if new_sid:
                    state["session_id"] = new_sid

                response = response or "(No response from Claude)"
                if len(response) > MAX_RESPONSE_LENGTH:
                    response = response[:MAX_RESPONSE_LENGTH] + "\n\n... (truncated)"

                # Track hash + send
                hashes = state.get("sent_hashes", [])
                hashes.append(text_hash(response))
                state["sent_hashes"] = hashes[-100:]

                if send_imessage(response, reply_target):
                    log(f"[reply → {reply_target}] {response[:80]}...")
                else:
                    log("[error] Failed to send")

                log_exchange(text, attachments, response, ts)
                advance_past_sent(chat_ids, state)
                save_state(state)
                break  # re-query with updated ROWID

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("\nStopping...")
            save_state(state)
            break
        except FDAError:
            delay = _chatdb.get_backoff_delay()
            log(f"[fda-backoff] Waiting {int(delay)}s before retrying...")
            time.sleep(delay)
        except Exception as e:
            log(f"[error] {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
