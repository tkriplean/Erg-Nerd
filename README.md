# Erg Nerd

A personal analytics dashboard for Concept2 rowing. Pulls your workout history from the Concept2 Logbook API, enriches it with fitness-level predictions from rowinglevel.com, and gives you views that the official logbook doesn't — pace curves, interval analysis, volume trends, and multi-model performance predictions. All data stays on your machine.

## Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- A Concept2 account with Logbook API credentials ([register an app](https://log.concept2.com/developers))

## Setup

```bash
# 1. Clone the repo
git clone <repo-url>
cd "Erg Nerd"

# 2. Copy the example env and fill in your Concept2 API credentials
cp .env.example .env
# Edit .env — set CONCEPT2_CLIENT_ID and CONCEPT2_CLIENT_SECRET

# 3. Install dependencies
poetry install
```

## Running

```bash
poetry run python app.py
```

The app opens at **http://localhost:8888**. On first launch it will redirect you to Concept2's OAuth page; after authorizing, your workouts sync automatically.

## Tabs

| Tab | What it shows |
|---|---|
| **Profile** | Your age, weight, gender, and max heart rate. Settings for switching to public profile. |
| **Volume** | Weekly / monthly training volume broken down by pace zone and HR zone |
| **Sessions** | Individual session explorer with HR, split, and stroke-rate overlays |
| **Intervals** | 2D grid of interval work by duration × work:rest ratio, with zone intensity |
| **Performance** | Ranked performances scatter (pace vs distance), simulation timeline, and multi-model prediction table |

## Local data files

The app stores everything locally — no cloud sync, no account required beyond the initial Concept2 OAuth.

| File | Contents |
|---|---|
| `.workouts.json` | Cached workout history from the Concept2 Logbook API |
| `.profile.json` | Your personal profile (age, weight, gender, max HR) |
| `.rowinglevel_cache.json` | Cached RowingLevel predictions (scraped from rowinglevel.com) |
| `.env` | Your Concept2 API credentials (never committed) |
