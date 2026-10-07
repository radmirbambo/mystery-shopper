# Mystery Shopper

An agent that measures how fast dental clinics call back a lead left on their website.
I built it to test the first hypothesis of our dental voice-AI startup: clinics lose
patients because web leads wait too long for a call back.

## Result

- Leads left at 56 Moscow clinics: 20 called back, 10 of them within 5 minutes.
- Patients mostly phone clinics instead of filling in forms, so the web-lead hypothesis
  didn't hold. We rebuilt the product around inbound calls:
  [dental-ai-receptionist](https://github.com/radmirbambo/dental-ai-receptionist).

## How it works

```
OpenStreetMap ──► discover/: every Moscow dental clinic with a website
                  triage each site: ok | captcha | no_form | dead
        │
        ▼
formbot/ (Playwright) leaves one lead per clinic ──► POST /form-submitted (timestamp)
        │
        ▼
clinic calls the operator's phone back
operator texts the Telegram bot: "Legenda 14:35"
bot fuzzy-matches the clinic ──► POST /callbacks ──► delta in minutes
        │
        ▼
call recording (iPhone ► Notes ► iCloud) ──► calls/: ffmpeg ► mlx-whisper ►
LLM rates the administrator (name, offer, booked or not, tone, score 0–10, quotes)
        │
        ▼
discover/lpr.py: site crawl ► INN, emails, people ► DaData ► decision-maker card
/lead in the bot: measurement + call analysis + draft email to the clinic owner
```

Rules the agent follows: one lead per clinic, captchas are never bypassed (a human
solves them in `--assist` mode), the agent never books an appointment.

## What was hard

- **Finding the form.** Clinic sites hide lead forms in iframes, modals and buttons,
  and phone inputs use JS masks. `formbot/fill_forms.py` scans frames, reveals hidden
  forms, types the phone through JS when masks block input, and falls back to an LLM to
  pick fields. Dry-run screenshots come first, a real submit needs explicit confirmation.
- **Matching a callback to a clinic.** The operator types whatever they heard on the
  phone. The backend matches with stemming, transliteration, aliases and known phone
  numbers, and the bot asks to pick from three options when unsure.
- **Running it for free.** Transcription is local (mlx-whisper large-v3-turbo). The LLM
  is a local Qwen2.5-3B served by `mlx_lm.server`, with OpenRouter as a fallback behind
  a 429 guard.

## Tech stack

- Python, Playwright
- FastAPI + SQLite, Excel report (`openpyxl`)
- Telegram Bot API (plain HTTP, no framework)
- mlx-whisper, `mlx_lm` (Qwen2.5-3B-Instruct 4-bit), OpenRouter
- OpenStreetMap Overpass, DaData
- launchd for schedules on macOS, pytest (51 tests)

## Project structure

```
discover/    OSM → candidates, site triage, promote to clinics.csv, decision-maker lookup (lpr.py)
formbot/     Playwright lead submitter: --dry, --assist, --auto
server/      FastAPI + SQLite: submissions, callbacks, transcripts, decision makers, /report.xlsx
telegram/    operator bot: log callbacks, /pending, /report, /lead, /wave, /pause
calls/       call recording → transcript → LLM analysis
voximplant/  inbound voice scenario, unused: renting a number needs a qualified e-signature
launchd/     macOS schedules: backend, bot, daily wave, morning digest, call watcher
daily.sh     one wave: promote new clinics → submit leads → report to Telegram
docs/        operator runbook (Russian)
```

## Running it locally

```bash
cp local.env.example local.env            # token, Telegram bot token, name and phone for leads
cp data/clinics.example.csv data/clinics.csv

for d in server telegram formbot calls; do     # calls/ needs Apple Silicon (mlx)
  python3 -m venv $d/.venv && $d/.venv/bin/pip install -r $d/requirements.txt -r requirements-dev.txt
done
formbot/.venv/bin/playwright install chromium

./run.sh backend                          # FastAPI on 127.0.0.1:8000
./run.sh bot                              # Telegram bot
./run.sh formbot --dry                    # fill forms, take screenshots, submit nothing
```

Tests:

```bash
server/.venv/bin/python -m pytest server
telegram/.venv/bin/python -m pytest telegram
formbot/.venv/bin/python -m pytest discover calls
```

The full operator runbook (waves, digest, call recording, decision-maker cards) is in
[docs/RUNBOOK.ru.md](docs/RUNBOOK.ru.md).
