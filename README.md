# Word ⇄ PDF Telegram Bot

Send a Word file (.doc, .docx, .odt, .rtf) and get a PDF back. Send a text PDF and get a .docx back.

## 1. Create the bot (2 minutes)
1. Open Telegram, chat with **@BotFather**, send `/newbot`.
2. Pick a name and username. Copy the **token** it gives you.

## 2. Test locally (optional)
Requires Python 3.11+ and LibreOffice installed.
```
pip install -r requirements.txt
export BOT_TOKEN=your-token      # Windows: set BOT_TOKEN=your-token
python bot.py
```

## 3. Deploy free on Render (no card needed)
1. Create a free GitHub account and push this folder to a new repository.
2. Sign up at https://render.com with GitHub.
3. **New → Blueprint** (or **New → Web Service**, runtime *Docker*), select your repo.
4. When asked, set the environment variable `BOT_TOKEN` to your BotFather token.
5. Deploy. The first build takes several minutes (it installs LibreOffice).

The bot registers its own webhook using Render's URL. Free services sleep after
15 minutes of inactivity; Telegram retries, so the first message after a quiet
period may take about 30–60 seconds. To keep it awake, add a free monitor at
https://uptimerobot.com pinging your Render URL every 10 minutes.

## Why not Firebase?
Firebase's free plan can't run this: Cloud Functions require a billing-enabled
plan and can't install LibreOffice, which Word → PDF needs. Render's free Docker
hosting can.

## Limits and notes
- 20 MB per file (Telegram Bot API limit), 8-second cooldown per user, 2 conversions at once.
- Scanned (image-only) and password-protected PDFs are not supported.
- PDF → Word layout fidelity depends on the PDF's complexity.
- Temporary files are deleted after every conversion.
