# tutor

Personal study tutor. This first part is the **Moodle connector**: it reads
teaching.kse.org.ua through your own logged-in Chrome, downloads course files, detects
changes (new activities, files, grades, feedback, deadlines, announcements), and gives
Claude live Moodle tools.

- Read-only: it only opens pages and downloads files. Nothing is ever submitted or posted.
- No Moodle API. It uses a separate Chrome profile that you log into once.
- Your login stays on your Mac. It is never uploaded anywhere.
- Assignment descriptions are stored locally but never returned to Claude, because
  some courses forbid putting assignment conditions into an AI prompt.

## Setup on the Mac (once, ~15 min)

1. **Install uv** (it brings its own Python; your system Python 3.9 is untouched):

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Google Chrome must be installed in /Applications.

2. **Get the code and install the `tutor` command:**

   ```sh
   git clone https://github.com/limnyks/tutor.git ~/tutor
   cd ~/tutor
   uv tool install --editable .
   ```

3. **Config** (optional but recommended): create `~/.tutor/config.json`:

   ```json
   {
     "google_account": "anhelina.oleksiuk.25@kse.org.ua",
     "files_dir": "~/Library/CloudStorage/GoogleDrive-oleksiuk.angelina@gmail.com/My Drive/Tutor/Moodle"
   }
   ```

   - `files_dir`: where course files go. Pointing it at your Google Drive folder puts
     them in Drive automatically (needs the Google Drive app). Default: `~/Tutor/Moodle`.
   - `google_account`: lets the reader pick the right account on Google's chooser page.
   - Other keys: `courses_ignore` (list of course ids or name fragments),
     `state_dir` (tutor-state repo, later), `request_delay`, `max_file_mb`, `lang`.

   Check with `tutor config`.

4. **Log in once:**

   ```sh
   tutor moodle-login
   ```

   A Chrome window opens on Moodle. Sign in with Google (and 2FA), check your courses
   are visible, then quit that window with **Cmd+Q**. The command then confirms the
   login works.

5. **Check the parsers against your real Moodle:**

   ```sh
   tutor moodle-inspect --course Probability
   ```

   It prints what it found: courses, activities, an assignment's dates, grades,
   upcoming events. **Send that printed output back in the design chat.** If
   something is missing, it also saved the pages in `~/.tutor/moodle/inspect/`; send
   only the one that's wrong (they contain your data).

6. **First full sync:**

   ```sh
   tutor moodle-sync
   tutor moodle-status
   ```

7. **Background sync** (at login, then every 3 h while the Mac is awake):

   ```sh
   tutor moodle-schedule install
   ```

   - Each run reads open assignments, quizzes, grades, announcements and deadlines,
     and downloads files from new activities. Once a day it also re-checks every
     known file for updates (or run `tutor moodle-sync --full`).
   - Log: `~/.tutor/moodle/sync.log` (kept to ~1 MB). Remove with
     `tutor moodle-schedule uninstall`. `tutor moodle-status` shows whether it's on.

8. **Connect to Claude** (full path, so it works however Claude is started):

   - Claude Code:

     ```sh
     claude mcp add --scope user moodle -- "$(which tutor)" moodle-mcp
     ```

   - Claude desktop app: Settings → Developer → Edit Config, add:

     ```json
     {
       "mcpServers": {
         "moodle": { "command": "/Users/YOUR_USER/.local/bin/tutor", "args": ["moodle-mcp"] }
       }
     }
     ```

     (`which tutor` prints the exact path.) Restart the app.

   Then ask e.g. "what's due this week on Moodle?" or "download new Databases files".

## Good to know

- **Moodle sees these visits as you.** Opening an assignment or file counts as viewing
  it, so activities that complete "on view" get marked complete, and teachers' logs
  show the visits. Nothing is ever submitted, posted or marked done by hand.
- **Skipping a course:** add a name fragment or id to `courses_ignore` in
  `~/.tutor/config.json`, e.g. `"courses_ignore": ["Educational grants"]`. Its
  deadlines still come from the Moodle calendar.
- **New term:** new courses appear by themselves (one "new course" event each). Old
  courses stay listed on Moodle and keep being read cheaply; add them to
  `courses_ignore` if you want them gone.
- **Assignment conditions stay local.** They're saved on the Mac but never returned
  to Claude, because some courses forbid putting them into an AI prompt.

## Notifications

After each background sync the Mac shows a notification for:
- a deadline within 48 h and again within 24 h (not for submitted assignments);
- a grade: only whether points were lost, never the points — ask the tutor what to fix;
- teacher feedback, a new assignment or quiz, a moved deadline, an announcement.

Each is sent once. More than 3 at a time are grouped into one. Between 23:00 and 08:00
nothing is shown; those wait for the first sync after 08:00. Change the hours with
`"notify_quiet_hours": [23, 8]` in `~/.tutor/config.json` (`null` = no quiet hours).

Check once: `tutor notify-test`. If nothing shows up: System Settings → Notifications →
Script Editor → Allow notifications.

## Study plan (Google Calendar)

The tutor plans your week on Sunday and re-plans the rest of it at the first session of
each day. It reads your busy time with Claude's Google Calendar connector, then fills the free
time in this order:
1. Sunday: weekly test, week review, and a month review on the last Sunday of the month.
2. Your own work on Moodle deadlines, earliest due first, finished a day before the deadline.
   Work due after the plan gets its share now.
3. Lessons, most-needed course first, one lesson per course a day.

The blocks go into your main Google Calendar (color "Sage", 10-minute reminder). Each one has
`tutor-plan` in its description; the tutor only ever deletes events carrying that marker.

- Day windows, daily maximum, block length, effort per assignment, course weights:
  `plan/settings.json` in tutor-memory.
- Your classes: `plan/timetable.json` (the tutor treats them as busy).
- Tell the tutor how much you did on an assignment ("2 h on CS310 Assignment 1"), so the
  plan stops re-planning finished work.
- `tutor plan` prints a draft in Terminal (without reading the calendar).

Claude Code gets the Google Calendar connector from your claude.ai account. Check with
`/mcp` that it's listed.

## Memory (the tutor-memory repo)

The tutor's memory lives in a separate private repo, `limnyks/tutor-memory`: the course
catalog (topics from the syllabi, grading, AI rules), an append-only log of answers,
lesson summaries and self-study reports, and the Moodle changes. Knowledge per topic,
reviews and the daily briefing are recomputed from the log.

Setup on the Mac (once):

```sh
git clone https://github.com/limnyks/tutor-memory ~/tutor-memory
tutor memory-init
claude mcp remove --scope user moodle   # replaced by the "tutor" connector of that folder
```

Study: `cd ~/tutor-memory && claude`. Each session starts with the briefing (deadlines,
Moodle changes, reviews due, unchecked self-study, last lesson) and ends by syncing memory
to GitHub. After every Moodle sync, the Moodle snapshot (without assignment conditions)
and its changes are committed there too.

Memory tools: `memory_briefing`, `memory_topics`, `memory_history`, `log_answer`,
`log_summary`, `log_study`. Commands: `tutor briefing`, `tutor memory-sync`.

## Connector tools

| Tool | Does |
|---|---|
| `moodle_status` | last sync, login state, errors |
| `moodle_courses` | enrolled courses |
| `moodle_course_contents` | sections and activities of a course |
| `moodle_deadlines` | due dates in the next N days |
| `moodle_assignments` | assignments and quizzes: dates, submission and grading status, grade (no descriptions) |
| `moodle_grades` | grade items, points, feedback |
| `moodle_files` | downloaded files and their paths (`graded: true` = don't put into an AI prompt) |
| `moodle_changes` | what changed recently |
| `moodle_sync` | read Moodle live now |
| `moodle_download` | download files for one course now |

Answers come from the last sync, so they are instant. `moodle_sync` and
`moodle_download` open Moodle live.

## When something goes wrong

- **"Login needed"** (macOS notification or `tutor moodle-status`): Google wants a
  password or 2FA again. Run `tutor moodle-login`, sign in, Cmd+Q. About 1 minute.
- **Browser fails to start:** the login Chrome window is probably still open. Quit it
  with Cmd+Q.
- **"Sync failed: No courses found"**: Moodle showed something else instead of your
  courses (maintenance, a policy to accept). Open Moodle in a browser; nothing was
  overwritten, and the next run continues normally.
- **"A background Moodle sync is running"**: wait a few minutes and repeat the command.
- **A course shows 0 activities or dates are missing:** Moodle's page layout differs
  from what the parser expects. Run `tutor moodle-inspect --course <name>` and send
  the output.

## Where things live

| What | Where |
|---|---|
| Chrome profile with your login | `~/.tutor/moodle-profile/` (Mac only; turn on FileVault) |
| Last snapshot, file list, status, change log | `~/.tutor/moodle/` |
| Course files | `files_dir`, as `<course>/<section>/<file>`; old versions in `.versions/` |

## Development

```sh
uv sync
uv run pytest
```
