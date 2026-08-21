# MediRemind — a medicine reminder driven by the prescription itself

A medicine reminder for patients and elderly people. **Photograph the
prescription; the alarms set themselves.** Nobody has to type out a dosing
schedule, and nobody has to remember what "1-0-1 after food" means at 8 in the
evening.

```
 photo of prescription ──▶ OCR ──▶ parser ──▶ [ human confirms ] ──▶ medicines
                                                                        │
                                                     scheduler ──▶ full-screen alarm
                                                                        │
                                                        taken / snoozed / skipped ──▶ adherence report
```

The confirmation step in the middle is deliberate and is the app's main safety
rule: OCR on a phone photo is never certain enough to start alarming an
80-year-old about a dose. Every parsed line carries a confidence score, and
anything doubtful is highlighted for a human to approve before a single alarm is
armed.

## Running it

```bash
./run.sh                 # sets up the virtualenv on first run
# open http://localhost:8000
```

Reading prescriptions from a photo needs the Tesseract engine:

```bash
sudo apt-get install tesseract-ocr      # Debian / Ubuntu
brew install tesseract                  # macOS
```

Without it the app still works end to end — the prescription is typed in
instead, and drives exactly the same parser, scheduler and alarms.

```bash
./.venv/bin/python -m pytest tests/ -q  # 84 tests
```

## What it reads

The parser understands how prescriptions are actually written, not a tidy
idealised form:

| On the prescription | What the app does |
|---|---|
| `1-0-1`, `1-1-1-1`, `1/2-0-1/2` | one alarm per non-zero slot, with half-tablet doses |
| `OD`, `BD`, `TDS`, `QID`, `HS`, `OM` | maps to morning / afternoon / evening / night / bedtime |
| `q8h`, `every 6 hours`, `8 hrly` | spreads doses evenly from the patient's wake-up time |
| `SOS`, `PRN`, `as needed` | saved as a medicine but **never** alarmed |
| `after food`, `before food`, `empty stomach` | shifts the alarm ±30–45 min around the meal |
| `x 5 days`, `for 2 weeks`, `5/7`, `x 3 months` | alarms stop by themselves when the course ends |
| `alternate day`, `once a week` | alarms only on the right days |
| `continue`, `long term` | ongoing, no end date |
| `Tab.` `Cap.` `Syp.` `Inj.` `T.` `C.` | dose read in tablets, ml, IU or drops as appropriate |
| `1-O-l` (OCR misreading `1-0-1`) | repaired before parsing |

Letterheads, patient demographics, dates and "follow up after 2 weeks" are
filtered out rather than turned into imaginary medicines.

## Alarm times follow the patient, not the clock

"After breakfast" means whatever time breakfast really is. Each patient has a
routine — wake, breakfast, lunch, evening, dinner, bed — and every dose is
anchored to it. Change breakfast from 08:00 to 10:00 in Settings and every
future morning alarm moves with it, immediately.

Doses are never written into the database in advance. A dose is a pure function
of *(medicine, date, routine)*, so it is computed on demand and only the
deviations — taken, skipped, snoozed — are stored. Editing a medicine or a meal
time is therefore correct for every future day with no rebuild and no stale
rows. Each dose gets a deterministic id (`<medicine_id>:<date>:<HHMM>`) so the
phone, the log and the alarm always agree on which dose they mean.

## Built for 70-year-old eyes and unsteady hands

- Base text is 21px, adjustable to 25px from the header; nothing important is
  below 1.25rem.
- Every tap target is at least 60px tall, and the alarm's buttons are 4.2rem —
  a shaky finger cannot hit "Skip" when it meant "Taken".
- Colour is never the only signal: every state also carries a word and an icon,
  because colour vision fades with age.
- A due dose takes over the **whole screen**, rings a low-pitched two-note chime
  (audible with age-related high-frequency hearing loss), speaks the medicine
  name out loud at 0.85× speed, vibrates, and raises a phone notification. It
  stays until somebody answers it.
- Works in light and dark, and installs to the home screen as a PWA. The shell
  is cached offline so today's plan opens with no signal.

## Beyond the alarm

- **Refill alerts** when a course is within five days of running out.
- **Adherence report** over 7/14/30 days, with a day-by-day bar chart and a
  one-tap SMS summary to the family member listed as caregiver.
- **Multiple patients** on one device, for a caregiver looking after more than
  one person.

## Layout

```
backend/
  parser.py      prescription text  -> structured medicines   (pure, heavily tested)
  scheduler.py   medicines + routine -> dose times and status (pure, no I/O)
  ocr.py         photo -> text, with preprocessing; degrades gracefully
  db.py          SQLite, no ORM
  main.py        FastAPI routes
frontend/        one HTML/CSS/JS PWA, no build step
tests/           84 tests, including a real photo -> alarms round trip
```

`parser.py` and `scheduler.py` hold all the medical logic and neither touches
the database or the network, which is what makes them worth testing this
thoroughly.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/prescriptions/parse` | photo or text → **draft** plan (saves nothing) |
| `POST` | `/api/prescriptions/confirm` | human-approved plan → medicines, alarms armed |
| `GET` | `/api/patients/{id}/schedule` | one day of doses with live status |
| `GET` | `/api/patients/{id}/due` | polled each minute: what is ringing now |
| `POST` | `/api/doses/action` | taken / skip / snooze / undo |
| `GET` | `/api/patients/{id}/adherence` | how well the plan is being kept |
| `GET` | `/api/patients/{id}/refills` | courses about to run out |

Full interactive docs at `/docs` while the server is running.

## Limits worth knowing

- OCR handles **printed** prescriptions well; genuinely handwritten ones are
  unreliable, which is exactly why the confirm-before-alarm step exists.
- Alarms fire while the app is open in the browser or installed as a PWA. Alarms
  when the app is fully closed need a push service or a native wrapper.
- The parser is tuned for prescriptions written in English shorthand
  (Indian/UK/Commonwealth conventions).
- This is a reminder tool. It does not check drug interactions and is not a
  substitute for the prescribing doctor.
