# Still Meera

A small local Telegram assistant for Meera, founder of Skinstinct. She posts a text or voice
note in her private Telegram channel. The bot scores it, looks for related industry headlines,
drafts a LinkedIn post in her voice, and sends it back to the channel for review.
**Publishing to LinkedIn is always manual.**

```
Telegram channel post ─► (voice? download + Gemini transcription)
                      ─► Gemini triage 0–10 ──► below threshold: held + reason (note kept)
                                            └─► Google News RSS headlines (optional)
                                                ─► Gemini draft in Meera's voice
                                                ─► Telegram: score · draft · sources · check before publishing
```

## Mac setup (one time)

These commands assume Terminal is open in this project folder.

1. **Python.** If `python3 --version` fails or asks to install developer tools, install
   [uv](https://docs.astral.sh/uv/) and let it provide Python:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source $HOME/.local/bin/env
   ```
2. **Create the environment and install dependencies:**
   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ```
   (Without uv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.)
3. **Create your private settings file:**
   ```bash
   cp .env.example .env
   open -e .env
   ```
   Fill in the values in TextEdit and save. `.env` is listed in `.gitignore`. Never commit it
   or paste its contents anywhere.

| Setting | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → your bot → API Token |
| `TELEGRAM_CHAT_ID` | Run `.venv/bin/python -m still_meera find-chat` (see below) |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `GEMINI_MODEL` | Pick a current model from https://ai.google.dev/gemini-api/docs/models that accepts audio input and JSON output. On 2026-09-24 that page listed Flash models such as `gemini-3.5-flash-lite` and `gemini-3.8-flash`. Check it yourself, because IDs change. |
| `TRIAGE_THRESHOLD` | Default `6`. This is a **product assumption**, not a validated cut-off. Raise it if too many weak drafts arrive; lower it if good notes are held. |

4. **Telegram channel setup:**
   - Add the bot to the private channel as an **administrator** with permission to **post messages**.
     Bots only receive `channel_post` updates from channels where they are admins.
   - Post any message in the channel, then run
     ```bash
     .venv/bin/python -m still_meera find-chat
     ```
     Copy the number (it starts with `-100`) into `TELEGRAM_CHAT_ID`.
   - Only this chat is processed. Posts from any other chat are ignored.

5. **Check settings** (prints "set"/"MISSING" only, never the values):
   ```bash
   .venv/bin/python -m still_meera check
   ```

## Run

```bash
.venv/bin/python -m still_meera run
```

Leave the Terminal window open; stop with Ctrl+C. State is in `data/still_meera.db` (SQLite),
so a restart does not reprocess anything.

**First live test:** in the channel, post a text note such as
*"Test: a customer asked why our cleanser has no fragrance. I want to explain how we decide what not to add."*
Within about a minute you should see four messages: score, draft, (sources, if a headline was used),
and "Check before publishing". Then try a short voice note.

### Commands you can post in the channel

| Post | Effect |
|---|---|
| `/draft 12` | Draft held note #12 anyway |
| `/retry 12` | One more attempt for a failed note (resumes from the failed step) |
| `/status` | Counts of notes by status |

### Terminal commands

```bash
.venv/bin/python -m still_meera notes              # list notes
.venv/bin/python -m still_meera notes --status held
.venv/bin/python -m still_meera show 12            # full record incl. transcript and draft
```

## Deploy on Vercel (runs without your Mac)

On Vercel the bot doesn't poll. Telegram sends each channel post to a **webhook**
(`app.py` → `still_meera/web.py`), and notes are stored in **Postgres**, because Vercel's
disk is wiped between requests. Finish the Mac setup above first so you have the bot
token and `TELEGRAM_CHAT_ID`. Get the chat ID *before* setting the webhook, because
`find-chat` uses polling.

1. **Import the repo** in Vercel (Add New → Project). No build settings are needed:
   Vercel finds `app.py` and installs `requirements.txt`.
2. **Add a database.** Project → Storage → create a **Neon** Postgres database and connect
   it to the project. This sets `DATABASE_URL`. The tables are created on first use.
3. **Set environment variables** (Project → Settings → Environment Variables):
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
   `TRIAGE_THRESHOLD`, plus two random strings you make up:
   `TELEGRAM_WEBHOOK_SECRET` and `CRON_SECRET`. To generate one:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Redeploy after adding them.
4. **Point Telegram at the deployment.** Put the same `TELEGRAM_WEBHOOK_SECRET` in your local
   `.env`, then:
   ```bash
   .venv/bin/python -m still_meera set-webhook https://YOUR-PROJECT.vercel.app
   .venv/bin/python -m still_meera webhook-info
   ```
   Post a test note in the channel. `webhook-info` shows Telegram's last delivery error, if any.
   Vercel → Logs shows the app side.

**Differences from local mode**

- **Retries.** Notes whose request was cut off, and automatic retries that are due, are picked
  up after every new post and by a daily Vercel Cron job (`vercel.json`; the Hobby plan allows
  one run per day). `/retry N` still works any time.
- **Time limit.** Each post is processed inside one request. That covers download,
  transcription, triage, news and draft. Check Project → Settings → Functions and make sure the
  max duration allows at least a minute or two. If Telegram doesn't get a reply in time it resends
  the post, and the duplicate is ignored.
- **One mode at a time.** Telegram won't allow polling while a webhook is set, so local
  `run` refuses to start. To go back to running on your Mac:
  `.venv/bin/python -m still_meera delete-webhook`. Local mode keeps its own SQLite data.
  To run `notes`/`show` against the Vercel database instead, set `DATABASE_URL` in `.env`.

## Offline demo (no credentials, no network, no charges)

```bash
.venv/bin/python -m still_meera demo
```

The demo runs the real pipeline code against mocked Telegram, Gemini and news.
Every input is labelled `[SAMPLE ...]` and every AI output `[MOCK ...]`. It shows a drafted note,
a held note, a voice note, a duplicate update, a wrong-chat post, a malformed AI response that
stops after the retry limit, and the bot's own output being ignored.

## Tests

```bash
.venv/bin/python -m pytest -v
```

All tests are **offline** and use mocks. They cover triage routing and threshold, duplicate updates
(including across a restart), channel filtering, bot-output loops, voice transcription failure,
download failure, malformed AI responses, permanent vs transient API errors, RSS outages,
Telegram send failures, crash recovery and message splitting. They do **not** prove the live Telegram
or Gemini integration. Do that with the first live test above.

## How it behaves

- **Triage.** Gemini returns `score`, `decision`, `reason`, `missing_information`, `risk_flags`
  (`unsupported_claims` and `private_customer_info`, kept separate) and `news_search_terms`.
  The app, not the model, applies the threshold. A high score is not a fact-check.
- **Held notes** are kept in SQLite with their reason and can be drafted later with `/draft N`.
- **News** comes from Google News RSS: title, source, date and link only. The bot never reads the
  articles and says so. If nothing genuinely relates, the draft has no news hook. Only headlines
  the draft actually used are listed as sources.
- **Voice.** Telegram voice notes (OGG/Opus) are downloaded through the Bot API (20 MB bot limit)
  and transcribed by Gemini. Telegram does not supply transcripts to bots.
- **Retries and cost.** Each step's result is saved before the next one starts, so a retry
  resumes where it failed and never re-pays for finished steps. Transient failures (network,
  429, 5xx, malformed JSON) retry automatically with backoff up to `MAX_ATTEMPTS` (default 2).
  Permanent failures (invalid key, unknown model, 4xx) stop immediately. The original note is never deleted.
- **Loop prevention.** Every message the bot sends is recorded and ignored if it comes back.
  Messages via or forwarded from bots, messages starting with the bot's own headers, and exact
  reposts of a stored draft are also ignored.
- **Privacy.** Gemini calls use `store=False` (no server-side interaction storage). The token is
  redacted from Telegram error messages; `check` never prints secrets.
- **SDK.** Uses the `google-genai` SDK's Interactions API (`client.interactions.create`), which
  Google's docs listed as GA and recommended for new projects when this was built.

## Voice references

- `voice/references.json`: the published pieces with their original IDs, generated by
  `.venv/bin/python -m still_meera extract-voice path/to/samples.pdf`
- `voice/voice_guide.md`: the reusable style guide injected into every draft prompt.

The samples are **style references only**, not scientific evidence. The prompt forbids reusing
their anecdotes, numbers, or customer stories in new drafts.
