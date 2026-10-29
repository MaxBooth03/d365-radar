# D365 Radar

Scans for new Microsoft Dynamics 365 roles in the Netherlands and Belgium 24/7, and shows them on a dashboard: company, role (the lead), when it was posted, product (F&O, CE, BC, Power Platform), contract or perm, and whether the poster is an agency.

Everything runs on free tiers: GitHub Actions does the scanning, and GitHub Pages hosts the dashboard.

## How it works

| Source | What it covers | How often |
|---|---|---|
| Adzuna API | Aggregates hundreds of NL and BE job boards | Full 3-month sweep twice a day, plus a quick check for brand-new roles every 6 hours |
| Jooble API | A second aggregator, with separate NL and BE sites | Every 6 hours (needs the `JOOBLE_KEY_NL` and `JOOBLE_KEY_BE` secrets) |
| Careerjet API | A third aggregator (careerjet.nl and careerjet.be) | Every 6 hours (needs the `CAREERJET_API_KEY` secret) |
| Employer careers feeds | D365 partners, ISVs and employers in NL and BE that publish their jobs through Recruitee, Greenhouse, Lever, Workable, SmartRecruiters or Personio | Every 30 minutes |

The dashboard refreshes itself every 5 minutes and whenever you return to the tab. There is also a Refresh button.

Any source without a key is simply skipped.

Each run:

- removes duplicates (the same role found on three boards shows once, and the direct careers link wins)
- tags each role by product, role type and contract or perm
- saves everything to `data/jobs.json`, which the dashboard reads

**Roles that are taken down are removed automatically.** Each full sweep re-checks every role from the last 3 months against what is still live.

- A role that is missing from its source gets marked "Possibly closed".
- If it is still missing on the next sweep, it is marked "Taken down" and hidden.
- If a check fails part-way, nothing is removed, so an outage can never wipe your list.
- Tick "Show taken down" to see recently closed roles and how long they were live.

**Adzuna free-tier budget:** the scanner tracks its own usage and stays under 2,400 calls a month and 240 a day, so it can never go over the free limit.

## Setup (about 15 minutes)

### 1. Create the repo
1. On GitHub, create a new **public** repository called `d365-radar`. Free GitHub Pages needs a public repo; see the privacy note below.
2. Upload every file from this folder, keeping the folder structure.
   - The `.github` folder is hidden on Mac and Windows. If it doesn't upload, use **Add file > Create new file**, type `.github/workflows/scan.yml` as the name, and paste in the contents of that file.

### 2. Get your free Adzuna key
1. Register at https://developer.adzuna.com and create an app.
2. In your repo, go to **Settings > Secrets and variables > Actions > New repository secret** and add:
   - `ADZUNA_APP_ID`
   - `ADZUNA_APP_KEY`

### 3. Turn on the dashboard
1. Go to **Settings > Pages**.
2. Set the source to **Deploy from a branch**, then choose branch `main`, folder `/ (root)`.
3. Your dashboard will be at `https://YOUR-USERNAME.github.io/d365-radar/`.

### 4. Start the scanner
1. Open the **Actions** tab and enable workflows if GitHub asks.
2. Click **D365 Radar scan > Run workflow**.

From then on it runs automatically every 30 minutes.

## Working your leads

The dashboard has five views:

- **All open:** every live role.
- **New:** posted in the last 24 hours, or found since your last visit.
- **To chase:** still live after 21+ days and not yet contacted. These are usually clients struggling to fill the role.
- **My leads:** roles you marked.
- **Hidden:** roles you marked as not relevant.

Each role has **Contacted**, **Follow up** and **Hide** buttons. Marks are saved in your browser, so use the same browser each day. The marks are included when you export to CSV.

## Optional extras

### Add target companies (the real edge)
Roles on a company's own careers page often appear before they reach the job boards, and before agencies get briefed. To track a company:

1. Find its careers page.
   - If the URL looks like `name.recruitee.com`, `boards.greenhouse.io/name`, `jobs.lever.co/name` or `apply.workable.com/name`, then `name` is the slug.
2. Add the company to a repo secret called `ATS_TARGETS`. A secret keeps your target-account list private. For example:

```json
[
  {"type": "recruitee", "slug": "companyname", "company": "Company Name", "default_country": "NL"},
  {"type": "lever", "slug": "othercompany", "company": "Other Co", "default_country": "BE", "region": "eu"}
]
```

You can also list companies under `ats_targets` in `config.json`, but anyone can see that file because the repo is public.

### Alerts to your phone or Teams
New roles can also be pushed to you as they're found. Add any of these as repo secrets:

- **Teams:** in a Teams channel, open **Workflows** and choose "Post to a channel when a webhook request is received". Copy the URL it gives you into a secret called `TEAMS_WEBHOOK_URL`.
- **Telegram:** create a bot with @BotFather, then add two secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

To add an "Open dashboard" button to alerts, add a repository **variable** (not a secret) called `DASHBOARD_URL` set to your Pages link.

Alerts skip agency postings and the first-run backlog.

### More coverage: Jooble and Careerjet keys
- **Jooble** issues one key per country.
  1. Request a key at https://nl.jooble.org/api/about and another at https://be.jooble.org/api/about.
  2. Add them as the secrets `JOOBLE_KEY_NL` and `JOOBLE_KEY_BE`.
- **Careerjet**
  1. Register as a publisher at https://www.careerjet.nl/partners/register/as-publisher, using your dashboard URL as the website.
  2. Copy the API key from your publisher account.
  3. Add it as the secret `CAREERJET_API_KEY`.

## Tuning
All tuning happens in `config.json`:

- search queries
- agency list (add competitors here)
- the lookback window (default 90 days)
- how many missed sweeps it takes to close a role
- how long closed roles stay visible
- whether Power Platform-only roles count

## Privacy note
With a public repo, the job data and `config.json` are public. These are public job ads anyway, but keep your target-company list in the `ATS_TARGETS` secret.

## Why not LinkedIn or Indeed directly?
Both block automated scraping, and scraping them breaks their terms. Adzuna and Jooble already pull from most of the same NL and BE boards.

For LinkedIn, set up LinkedIn's own job alerts to your email instead.
