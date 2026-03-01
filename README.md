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

## About

Made by [Logan Currie](https://www.tiktok.com/@loganinthefuture) with Claude Code.

Part of my series on building personal AI operating systems — [Captain's Log on Substack](https://loganinthefuture.substack.com/).
