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

1. **Install uv** (it brings its own Python 3.12; your system Python 3.9 is untouched):

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

   Log: `~/.tutor/moodle/sync.log`. Remove with `tutor moodle-schedule uninstall`.

8. **Connect to Claude:**

   - Claude Code:

     ```sh
     claude mcp add --scope user moodle -- tutor moodle-mcp
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
