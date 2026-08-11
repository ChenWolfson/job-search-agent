# Google setup — one time, ~15 minutes

Everything happens in the browser, with your Google account.

---

## What we ask Google for, and why

| Scope | Used by | Why it's needed | What it does **not** allow |
|---|---|---|---|
| `spreadsheets` | `tracker.py` | write to the tracker sheet | — |
| `gmail.send` | `tracker.py` | send the daily digest | **send only.** Cannot read your mail. Recipient is hardcoded |
| `drive.readonly` | `tracker.py` | optional (legacy) | **read only** |
| `gmail.readonly` | `fetch_alerts.py` | read job-alert digests | **read only.** Separate token (`token_read.json`) |

The split is deliberate: the component that writes to your sheet cannot read
your email, and the component that reads your email cannot write anywhere.

---

## Step 1 — new project
1. Go to https://console.cloud.google.com
2. At the top, next to the logo, click the project picker → **New Project**
3. Name: `Job Tracker` → **Create**
4. Make sure the new project is selected in the picker

## Step 2 — enable the APIs
For each one: search in the top bar, open, click **Enable**.
- `Google Sheets API`
- `Gmail API`
- `Google Drive API` (optional)

## Step 3 — consent screen
1. Side menu: **APIs & Services** → **OAuth consent screen** (in the new console: **Google Auth Platform**)
2. If asked about Audience: choose **External** → **Create**
3. App name: `Job Tracker`. User support email + Developer contact: your email. → **Save**

## Step 4 — publish (important! don't skip)
On the **Audience** screen, if the status is `Testing`, click **Publish app** → **Confirm**.

**Why this is critical:** in `Testing` mode Google revokes the connection **every
7 days** — the automation would break weekly and demand a re-login. Once
published, the connection is permanent.

Google will show an "unverified app" warning when you authorize — that's normal
and expected: this is your own private app, with a single user (you).

## Step 5 — create the credentials
1. **APIs & Services** → **Credentials**
2. **+ Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app** → name: `Job Tracker CLI` → **Create**
4. **Download JSON**

## Step 6 — save the file
Save the downloaded file as `credentials.json` inside a `.secrets/` folder at
the project root (create the folder if needed). It is excluded from git.
**This file is like a house key — never share it.**

## Step 7 — the spreadsheet
1. Create a new Google Sheet with two tabs, named exactly: **`Job Tracker`** and
   **`Change Log`**. (If you rename them, change `TAB_JOBS` / `TAB_LOG` in
   `tracker.py` to match.)
2. In the `Job Tracker` tab, paste the 21 column headers into row 1 — copy them
   straight from `COLUMNS` in `tracker.py`. In the `Change Log` tab, paste
   `LOG_COLUMNS` into row 1.
   **The order matters and is positional.** Never insert a column by hand later:
   the code addresses columns by index and will silently read and write the wrong
   ones. See ARCHITECTURE.md.
3. Copy `config.example.json` to `config.json` and fill in the spreadsheet id
   (from the URL) and the two tab ids (the `gid=` in each tab's URL). Set
   `home_city` and `location_tokens` for where you live.
4. Set `MAIL_TO` in `tracker.py` to your own address.
5. Run `python tracker.py reformat` — applies all formatting, dropdowns, and
   conditional-colour rules.

## Step 8 — first run
The first run of any command opens a browser window:
1. Pick your Google account
2. "Google hasn't verified this app" → **Advanced** → **Go to Job Tracker (unsafe)**
   (normal — "unsafe" here just means "Google didn't review this app", and the
   app is your own script)
3. **Allow** the scopes

Done. From here it runs on its own.
