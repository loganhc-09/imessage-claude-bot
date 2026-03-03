# iMessage Claude Bot

Text yourself on iMessage. Claude responds. No apps, no APIs, no monthly fees.

**[View the setup guide →](https://loganhc-09.github.io/imessage-claude-bot/)**

## What This Is

A Python script that turns your iMessage self-chat into a Claude Code interface. It monitors your Mac's Messages database, sends your texts to Claude Code CLI, and replies back in the same thread.

You get a full AI agent in your pocket — file access, web search, code execution, image understanding — all from the app already on your phone.

## Quick Start

```bash
git clone https://github.com/loganhc-09/imessage-claude-bot.git
cd imessage-claude-bot
cp .env.example .env
# Edit .env with your Apple ID and phone number
python3 imessage-bot.py
```

## Requirements

- Mac with macOS 13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- Full Disk Access granted to `/usr/bin/python3`
- An iMessage self-chat (text yourself once to create it)

## Troubleshooting: FDA Loss

The most common issue is macOS intermittently revoking Full Disk Access (FDA) from the Python process. When this happens, the bot can't read `chat.db` and stops processing messages.

**Symptoms:** Bot log shows `[fda-lost] Authorization denied for chat.db` and stops responding.

**What's happening:** macOS TCC (Transparency, Consent, and Control) can revoke FDA from long-running processes, especially after they spawn subprocesses (like the Claude CLI). The bot now handles this automatically:

1. **Fast detection** — fails in 6 seconds instead of spinning for 2.5 minutes
2. **Exponential backoff** — waits 30s, 60s, 120s, 300s instead of burning CPU
3. **Self-notification** — texts you after 2 min of downtime with instructions
4. **Self-restart** — exits after 10 min so launchd spawns a fresh process (which usually fixes it)

**If it persists:** System Settings → Privacy & Security → Full Disk Access → toggle Python off, then on again. Or manually restart: `launchctl kickstart -k gui/$(id -u)/com.claude.imessage-bot`

## About

Made by [Logan Currie](https://www.tiktok.com/@loganinthefuture) with Claude Code.

Part of my series on building personal AI operating systems — [Captain's Log on Substack](https://loganinthefuture.substack.com/).
