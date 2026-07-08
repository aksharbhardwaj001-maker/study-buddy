# Study Buddy

Paste a YouTube link, say how far you've watched, get a recap + quiz on just that new segment.

## ⚠️ First: rotate your API key

The key that was in the original script has been shared in plaintext and should be treated as
compromised. Go to https://openrouter.ai/keys, delete/rotate the old key, and use the new one below.
Never hardcode a key inside a script that might be shared, committed to git, or deployed.

## Setup

```bash
cd study_buddy
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Set your API key as an environment variable (don't put it in the code):

```bash
# macOS/Linux
export OPENROUTER_API_KEY="your-new-key-here"

# Windows (PowerShell)
$env:OPENROUTER_API_KEY="your-new-key-here"
```

## Run it

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## How it works

- `study_buddy.py` — your original logic (transcript fetching, recap/quiz generation via
  OpenRouter, grading), refactored to return data instead of printing/using `input()`.
- `app.py` — the Flask routes: paste link → enter watched time → recap & quiz → graded results.
- `templates/` — the four pages.
- `static/style.css` — styling.
- `video_memory.json` — created automatically; tracks watch progress and quiz scores per video,
  same as the original script.

## Notes

- Progress/session data is currently kept in server memory per browser session — if you restart
  the server mid-quiz, you'll need to paste the link again (per-video watch progress in
  `video_memory.json` is unaffected).
- This is set up for local/personal use. If you want to put it on the public internet, you'd want
  to add rate limiting and swap the in-memory session store for something persistent (e.g. Redis),
  since restarting the server clears in-progress sessions.
