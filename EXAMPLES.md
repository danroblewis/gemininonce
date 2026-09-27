# Example projects

Projects for trying `gemininonce` on real problems, and for spotting regressions when its prompts or
checks change. They all depend on **public APIs that need no key**, because external connections
are the hard part: finding current docs, mocking the APIs in unit tests, calling them for real in
e2e tests, and handling rate limits, flaky responses and changing formats.

- **1–6: build from an idea** with `gemininonce build`.
- **7–10: migrations.** First build a project pinned to an *old* library or API version, then
  upgrade it and let the fix loop repair what broke. That's the everyday job of fixing API version
  changes and incompatibilities.

## Setup

Give each example its own folder and virtual environment, with pytest and the packages it will
probably need installed up front. Gemini may still suggest `pip install` commands, which you
approve at the prompt. Unattended runs (`--accept`, no terminal) skip them, so the installs up front
matter there.

```sh
mkdir -p ~/gemininonce-examples && cd ~/gemininonce-examples

# new <folder> [packages...]: make the folder, a venv in it, and install pytest + the packages
new() { mkdir -p ~/gemininonce-examples/"$1" && cd ~/gemininonce-examples/"$1" \
  && python3 -m venv .venv && . .venv/bin/activate && pip install -q pytest "${@:2}"; }
```

The commands use `--anonymous`, meaning signed-out free-tier Gemini (Flash-Lite). Drop it to use
your account, where the spec and test plan get Pro. Add `--accept` to run without discussing the
spec, plan and tests, which is useful for comparing runs. What to record for each run:
- the stage where it got stuck, if any
- the number of messages and the cost
- the number of tests
- whether `pytest -q` passes afterwards

---

## Build projects

### 1. Dependency freshness checker

Read a `requirements.txt` or `package.json`, look up the latest versions on **PyPI** and the **npm
registry**, and report which packages are outdated, yanked or deprecated, with release dates.

**Hard parts:** two registries with different JSON shapes; yanked releases; version ordering
(`1.10 > 1.9`, pre-releases); network failures.

```sh
new dependency-freshness httpx packaging
gemininonce build "A Python CLI, depcheck, that reads a requirements.txt or a package.json and, for each \
dependency, looks up the latest version on PyPI (https://pypi.org/pypi/<name>/json) or the npm registry \
(https://registry.npmjs.org/<name>), then prints a table: name, pinned or required version, latest version, \
latest release date, and a status of up-to-date, outdated, yanked or deprecated. Compare versions with the \
packaging library (PEP 440) for PyPI and semver for npm, ignore pre-releases unless --pre is given, exit \
with code 1 if anything is outdated, and handle packages that don't exist and network errors gracefully." \
  --dir . --anonymous
```

### 2. GitHub release notes digest

For a list of repos, fetch releases since a date via the **GitHub REST API**, then group and
summarize breaking changes.

**Hard parts:** pagination via `Link` headers; the unauthenticated limit of 60 requests per hour;
API versioning (`X-GitHub-Api-Version`).

```sh
new github-releases httpx
gemininonce build "A Python CLI, reldigest, that takes GitHub repos as owner/name and a --since date, fetches \
their releases from the GitHub REST API (GET /repos/{owner}/{repo}/releases, following Link-header \
pagination, sending the X-GitHub-Api-Version header), keeps releases published on or after the date, and \
prints a Markdown digest per repo: version, date, and the release-note lines that mention breaking changes \
(BREAKING, deprecated, removed, migration). It works without a token (60 requests/hour), uses GITHUB_TOKEN \
if set, and reports rate limiting clearly using the X-RateLimit-Remaining and X-RateLimit-Reset headers." \
  --dir . --anonymous
```

### 3. Earthquake watcher

Poll the **USGS GeoJSON feed** and alert on quakes within X km of a point above magnitude M,
remembering which quakes were already seen.

**Hard parts:** GeoJSON parsing; distance math; time windows; deduplication across polls.

```sh
new earthquake-watcher httpx
gemininonce build "A Python CLI, quakewatch, that polls the USGS earthquake GeoJSON feed \
(https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson) and prints an alert for each \
earthquake within --radius-km of --lat/--lon with magnitude at least --min-mag: time (UTC and local), \
magnitude, place, distance in km (haversine), and the USGS event URL. It remembers alerted event ids in a \
JSON state file so each quake is reported once across runs, supports --once or --every SECONDS, and \
handles feed errors and malformed features without crashing." \
  --dir . --anonymous
```

### 4. Travel planner (three APIs chained)

Geocode places with **Open-Meteo's geocoding API**, fetch weather, convert currencies with
**Frankfurter** exchange rates, and print a trip summary.

**Hard parts:** three APIs chained together, handling one failing partway, timezones, caching.

```sh
new travel-planner httpx
gemininonce build "A Python CLI, tripbrief, that takes a list of cities with dates and a home currency, and \
for each stop: geocodes the city with the Open-Meteo geocoding API (https://geocoding-api.open-meteo.com/v1/search), \
fetches the daily forecast (max/min temperature, precipitation) for those dates from the Open-Meteo forecast \
API in the city's own timezone, and converts a daily budget from the home currency into the local currency \
using Frankfurter exchange rates (https://api.frankfurter.app/latest). Print one summary per stop and a trip \
total. Cache API responses on disk for an hour, and if one API fails, still print the rest with that part \
marked unavailable." \
  --dir . --anonymous
```

### 5. Wayback snapshot tool

Find the snapshot of a URL closest to a date with the **Wayback Machine availability and CDX APIs**,
and diff two snapshots' text.

**Hard parts:** slow, flaky responses (good for testing retries and timeouts); large payloads;
fuzzy "closest" logic.

```sh
new wayback-tool httpx beautifulsoup4
gemininonce build "A Python CLI, wayback, with two commands. 'closest URL DATE' finds the Internet Archive \
snapshot closest to DATE using the availability API (https://archive.org/wayback/available) and falls back \
to the CDX API (https://web.archive.org/cdx/search/cdx) when needed, printing the snapshot URL and \
timestamp. 'diff URL DATE1 DATE2' fetches the two closest snapshots, extracts their visible text (dropping \
the Wayback toolbar), and prints a unified diff. Use timeouts and retries with backoff, because the \
archive is slow and sometimes fails, and report clearly when no snapshot exists." \
  --dir . --anonymous
```

### 6. Library availability checker

Search **Open Library** by ISBN or title, show editions, and check reading availability.

**Hard parts:** inconsistent data with fields missing everywhere; search versus lookup endpoints.

```sh
new open-library httpx
gemininonce build "A Python CLI, bookfind, that looks up books on Open Library: 'isbn ISBN' uses the ISBN \
API (https://openlibrary.org/isbn/<isbn>.json) and 'search TITLE [--author A]' uses the search API \
(https://openlibrary.org/search.json). For each work it prints title, authors (resolving author keys to \
names), first publish year, number of editions, and whether it can be borrowed or read online, using the \
search results' availability fields. Many fields are missing on real records, so every field must be \
optional in the output, and it should handle unknown ISBNs and network errors cleanly." \
  --dir . --anonymous
```

---

## Migrations: code that worked until something changed

Each one takes three steps: **build** the project against the old version, **break** it by
upgrading, and **fix** it with the fix loop. The fix commands pass `--network` so the e2e tests can
reach the real API; the plain fix loop is offline by default. Check that the build step's tests pass
before you break anything.

### 7. Pydantic v1 → v2

A small API client whose Pydantic models parse real API responses. The v1 → v2 upgrade renamed a
lot: `.dict()` → `.model_dump()`, `parse_obj` → `model_validate`, validators, `class Config`.

**Why it's a good test:** it's one of the most common real-world migrations, with many small breaks
spread across the code.

```sh
new pydantic-migration httpx "pydantic>=1.10,<2"
gemininonce build "A Python library and CLI, ghrepo, that fetches a GitHub repository and its latest \
releases from the GitHub REST API and parses them into Pydantic v1 models (use Pydantic v1 APIs only: \
BaseModel with class Config, @validator, parse_obj, .dict(), .json()), then prints a summary. Pin \
pydantic>=1.10,<2 in requirements.txt." --dir . --anonymous

pip install -U "pydantic>=2"      # break it
pytest -q                          # see what broke
gemininonce . -t "pytest -q" --network --anonymous \
  -m "We upgraded to Pydantic v2. Migrate the code to the Pydantic v2 API (no v1 compatibility shims) \
      and update requirements.txt."
```

### 8. SQLAlchemy 1.4 → 2.0

A service that caches API results in SQLite, written in the 1.x style: `engine.execute()`,
`select([...])` with a list, and implicit autocommit. Those were removed in 2.0.

**Why it's a good test:** new query style and session handling, and the tests exercise real
database behavior.

```sh
new sqlalchemy-migration httpx "sqlalchemy>=1.4,<1.5"
gemininonce build "A Python CLI, pypicache, that fetches package metadata from the PyPI JSON API \
(https://pypi.org/pypi/<name>/json) and caches it in SQLite with SQLAlchemy 1.4 in the 1.x style: \
engine.execute() for queries, select([table.c.col, ...]) with a list, Table objects with MetaData, and \
implicit autocommit. It has commands to fetch, list cached packages, and show a package from cache with a \
max age. Pin sqlalchemy>=1.4,<1.5 in requirements.txt." --dir . --anonymous

pip install -U "sqlalchemy>=2"    # break it
pytest -q
gemininonce . -t "pytest -q" --network --anonymous \
  -m "We upgraded to SQLAlchemy 2.0. Migrate to the 2.0 API (connections and sessions with explicit \
      commits, select() without lists) and update requirements.txt."
```

### 9. Swapping the HTTP client: sync requests → async httpx

A tool that calls a public API with `requests`, converted to `async` with `httpx`.

**Why it's a good test:** async changes ripple through the code and the tests, along with timeouts
and retries.

```sh
new http-client-swap requests httpx pytest-asyncio
gemininonce build "A Python library and CLI, hnfront, that fetches the current Hacker News front page from \
the official Firebase API (https://hacker-news.firebaseio.com/v0/topstories.json and /v0/item/<id>.json) \
using the requests library (synchronous), with a timeout and one retry per request, and prints the top N \
stories with score, comment count and domain." --dir . --anonymous

gemininonce . -t "pytest -q" --network --anonymous \
  -m "Replace requests with httpx and make the library async (httpx.AsyncClient, fetching items \
      concurrently with asyncio.gather, keeping the timeout and retry). The CLI stays synchronous from the \
      user's point of view. Update the tests (pytest-asyncio) and requirements.txt."
```

### 10. An upstream API change: PyPI's deprecated `releases` field

PyPI's JSON API (`/pypi/<name>/json`) has deprecated its `releases` field in favor of the "simple"
JSON API (PEP 691: `https://pypi.org/simple/<name>/` with
`Accept: application/vnd.pypi.simple.v1+json`). The first step builds a tool on the old field; the
fix step moves it to the new API.

**Why it's a good test:** it's exactly "the API changed under me". Gemini has to find the new API
itself (research), and the response shape is completely different.

```sh
new pypi-api-change httpx packaging
gemininonce build "A Python CLI, pkghistory, that lists every released version of a PyPI package with its \
upload date and whether it was yanked, oldest first, using the 'releases' field of the PyPI JSON API \
(https://pypi.org/pypi/<name>/json). Options: --since DATE and --latest N." --dir . --anonymous

gemininonce . -t "pytest -q" --network --anonymous \
  -m "The 'releases' field of PyPI's /pypi/<name>/json API is deprecated. Switch to the PyPI simple JSON \
      API (PEP 691 and PEP 700: https://pypi.org/simple/<name>/ with the Accept header \
      application/vnd.pypi.simple.v1+json), which lists files with upload times and yanked status per file; \
      derive versions and their dates from it. Keep the CLI's output the same. Update the tests too."
```

In #9 and #10 the fix step changes behavior on purpose, so the tests have to change with it.
That's why those commands don't lock anything.
