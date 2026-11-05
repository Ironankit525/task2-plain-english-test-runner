# Plain-English Test Runner

Write a test case in plain English, and this tool runs it in a real browser and reports whether it passed. Nothing is written for a particular website, so it works on sites it has never seen.

```bash
nltest "Log in as locked_out_user and verify the products page is shown" --url https://www.saucedemo.com
```

```text
   1. type [1] locked_out_user
   2. type [2] secret_sauce
   3. click [3]
   4. finish

[FAIL] The products page is shown.
       Evidence: "Epic sadface: Sorry, this user has been locked out."

Result: FAIL
```

Read [APPROACH.md](APPROACH.md) for how it works, measured results and limitations.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/), or plain `pip` (see below)
- A Gemini API key, free from [Google AI Studio](https://aistudio.google.com/apikey)

## Set up

```bash
git clone <this repository>
cd task2-plain-english-test-runner
uv sync
uv run playwright install chromium
cp .env.example .env
```

Then put your key in `.env`:

```text
GEMINI_API_KEY=your-key-here
```

<details>
<summary>Using pip instead of uv</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
export GEMINI_API_KEY=your-key-here
nltest "Select Option 2 from the dropdown and verify it's selected" --url https://the-internet.herokuapp.com/dropdown
```

</details>

## Run a test

```bash
uv run --env-file .env nltest "Select Option 2 from the dropdown and verify it's selected" \
  --url https://the-internet.herokuapp.com/dropdown
```

Add `--headed` to watch the browser.

| Option | Purpose | Default |
|---|---|---|
| `--url <url>` | Page to start on | None. The test case can name a full address instead |
| `--credentials <file>` | File of named logins | None |
| `--provider <name>` | `auto`, `gemini` or `openai` | `auto` |
| `--model <name>` | Model to try first | `gemini-3.5-flash` |
| `--max-steps <n>` | Most steps to take | `25` |
| `--time-limit <seconds>` | Most time to run | `600` |
| `--headed` | Show the browser | Off |
| `--output <folder>` | Where reports go | `runs` |

## Tests that need a login

Logins aren't built in. Copy `credentials.example.json` to `credentials.json` and fill in your own:

```json
{
  "customer": { "email": "you@example.com", "password": "your-password" }
}
```

Then pass the file:

```bash
uv run --env-file .env nltest "Log in as a customer and verify the dashboard is shown" \
  --url https://phptravels.net --credentials credentials.json
```

The role names are up to you. `customer` above lets a test case say "as a customer". The AI model only ever sees placeholders such as `{{customer_password}}`, and the real values never reach it or the reports.

For phptravels, request logins at [phptravels.com/demo](https://phptravels.com/demo). Logins written in the test case, or shown publicly on the page, work without a file. With no login available, the tool stops and says so rather than inventing one.

## Read the result

| Exit code | Result | Meaning |
|---|---|---|
| 0 | PASS | Every check was confirmed on the page |
| 1 | FAIL | The website didn't do what the test case expects |
| 2 | ERROR | The test couldn't be carried out, such as a CAPTCHA, a missing login, or a site or AI outage |

Each run saves a folder in `runs/` with `report.json`, a screenshot of every step and a full-page final screenshot.

## AI models

The tool tries `gemini-3.5-flash`, then `gemini-3.5-flash-lite`, then `gemini-3.1-flash-lite`. It moves on when a model is busy, retired or out of daily quota. Free Gemini keys have low limits, so expect it to reach the lite models quickly, and to pause for 25 seconds if it hits a per-minute limit. If every model is out of quota, the run ends with ERROR.

To use another provider, or add one as a backup, set these in `.env`. Any service with an OpenAI-style `/chat/completions` endpoint works if its model can read images:

```text
NLTEST_OPENAI_BASE_URL=https://your-provider.example/v1
NLTEST_OPENAI_API_KEY=your-key-here
NLTEST_OPENAI_MODEL=your-model-name
```

`--provider auto` then uses it after Gemini, and `--provider openai` uses it alone.

## Run the tests

These need no API key, because a scripted stand-in replaces the AI model:

```bash
uv run pytest
```

To run the real example suite in `examples/suite.json` (this uses your AI quota):

```bash
uv run --env-file .env python scripts/run_suite.py
uv run --env-file .env python scripts/run_suite.py --repeat 3 --credentials credentials.json
```

Cases needing a login are skipped unless you pass `--credentials`.

## Project layout

| File in `src/nltest/` | Purpose |
|---|---|
| `cli.py` | Command line |
| `config.py` | Settings and credentials |
| `planner.py` | Turns the sentence into a goal and checks |
| `agent.py` | The observe, decide, act loop |
| `observe.py` | Builds the numbered page summary |
| `actions.py` | Runs each action in the browser |
| `verdict.py` | Confirms evidence against the real page |
| `llm.py` | The only file that talks to AI providers |
| `report.py` | Console output and the JSON report |
| `prompts/system.md` | Instructions for the AI model |
