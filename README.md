# Measles watch

Small static tracker for measles activity around Pasadena/Bowie, MD and
Sedona, AZ (Yavapai + Coconino counties). A scheduled GitHub Action fetches
official health-department pages and regional news, diffs against the last
run, commits `site/data.json`, and Netlify redeploys the page. Optional push
notifications via [ntfy](https://ntfy.sh) when something actually changes.

No dependencies — Python 3.11+ stdlib only. No build step.

## Setup (~5 minutes)

1. Push this folder to a GitHub repo.
2. In Netlify: **Add new site → Import from Git**, pick the repo. The included
   `netlify.toml` publishes `site/` with no build command. Done — every data
   commit triggers a redeploy.
3. Run it once so the page has data: **Actions → Update measles data →
   Run workflow** (or `python3 fetch_measles.py` locally and push).
4. Optional notifications: pick a private ntfy topic name (treat it like a
   password — anyone who knows it can subscribe), add it as a repo secret
   `NTFY_TOPIC`, and subscribe to that topic in the ntfy app. Self-hosting
   ntfy? Add `NTFY_SERVER` too. No topic set = no pushes, page still updates.

## How it decides something is "new"

- **Watch pages** (MDH, AZDHS, county pages): extracts visible text, diffs
  line-by-line against `state.json`, and only alerts on added lines that
  mention cases / exposures / wastewater / outbreak etc. Rotating banner
  noise (heat warnings and similar) is filtered out.
- **News**: Google News RSS per region query; links it hasn't seen before
  are flagged and pushed.
- First run seeds state silently — no notification storm.

## Schedule

Daily at 9am ET, plus Tuesday afternoons right after AZDHS's weekly 3pm MST
data update. Edit the cron lines in `.github/workflows/update.yml` to taste.

## Known limitations

- `coconino.az.gov`, `yavapaiaz.gov`, and `cdc.gov` block some datacenter
  IPs (403). The script degrades gracefully (`unreachable last run` on the
  page), and county press releases still arrive via the Google News queries —
  Coconino's own newsflashes show up there. If GitHub's runners get through,
  you'll get the direct diffs too.
- Google News RSS needs no API key but is rate-limit-tolerant, not
  rate-limit-proof. The current 4 queries/run is well under any threshold.
- Page-diffing is heuristic. Treat alerts as "go look," not as data.

## Tuning

- Add/edit regions, queries, and watch pages in the `REGIONS` list at the top
  of `fetch_measles.py`.
- Alert sensitivity: `RELEVANT` and `NOISE` regexes.
- Homelab alternative: skip GitHub Actions entirely and run
  `fetch_measles.py` from a cron container, then `netlify deploy --prod
  --dir=site` or just serve `site/` behind Tailscale.
