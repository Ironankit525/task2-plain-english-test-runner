# Approach and Limitations

## The idea

A fixed test script knows one website. This tool instead works the way a person does: it looks at the page, decides the next step, does it, and looks again. So nothing about any website is written into the code.

The important design choice is the split of responsibilities: **the AI decides what to do, but code carries out the actions and has the final say on the result.** AI models can claim success without proof, and a test tool that reports false passes is worse than none.

## How it works

1. **Plan.** The AI turns the sentence into a goal and a list of checks. It's told never to change what the test case expects, even if that seems likely to fail.
2. **Observe.** Code builds a numbered list of interactive elements, including those in iframes and shadow DOM, plus the visible text and a screenshot.
3. **Decide.** The AI returns one action as validated JSON, such as `click`, `type` or `finish`.
4. **Act.** Playwright carries it out. If it fails, the error goes back to the AI, which tries something else.
5. **Repeat** until the AI finishes or gives up, or a limit is reached.
6. **Verify.** Code checks the AI's evidence against what the browser really showed.

## What keeps the results honest

| Risk | Protection |
|---|---|
| False PASS | Each check needs a quote, and code searches for it in text the browser really showed. A made-up quote can't be found. Every check must be covered, and the last must hold on the final page |
| False FAIL from a slow page | A FAIL is only accepted after waiting 3 more seconds and checking again |
| Blaming the website for a tool fault | Three results, not two. ERROR means the test couldn't be carried out, and any crash in the tool is an ERROR |
| Getting stuck | Limits on steps and time, and detection of repeated actions |
| Leaked passwords | The AI only sees placeholders such as `{{customer_password}}`. Code swaps in the real value when typing, and removes it from prompts and reports |
| Instructions hidden in a page | Page content is marked as untrusted data, and the AI is told to ignore instructions in it |
| Missing logins | The tool stops with ERROR rather than inventing one |
| AI outages | Retries, then backup models. A model out of daily quota is skipped for the rest of the run |

## Results

I ran 12 test cases on three sites, three times each, with Gemini. The set includes tests that **should** fail or stop, because a tool that always says PASS would otherwise look perfect.

| # | Site | Test | Expected | Matched |
|---|---|---|---|---|
| 1 | phptravels | Log in as a customer, verify the dashboard is shown | PASS | 3 of 3 |
| 2 | phptravels | Wrong password, verify an error appears | PASS | 3 of 3 |
| 3 | phptravels | Wrong password, verify the dashboard is shown | FAIL | 3 of 3 |
| 4 | phptravels | Search for hotels in Dubai | PASS | **2 of 3** |
| 5 | saucedemo | Add a backpack, cart shows 1 | PASS | 3 of 3 |
| 6 | saucedemo | Locked-out user sees products | FAIL | 3 of 3 |
| 7 | saucedemo | Complete checkout | PASS | 3 of 3 |
| 8 | the-internet | Valid login | PASS | 3 of 3 |
| 9 | the-internet | Dropdown selection | PASS | 3 of 3 |
| 10 | the-internet | Page says "Welcome to Mars" | FAIL | 3 of 3 |
| 11 | Unreachable site | Any test | ERROR | 3 of 3 |
| 12 | phptravels | Customer login with none supplied | ERROR | 3 of 3 |

**35 of 36 runs gave the expected result, with no false PASS results.** A typical test took about 20 seconds, and the longest about 75. On average a test used 6 AI calls and about 18,000 input tokens.

Case 1 is your example, and it took 6 steps each time, ending on `/dashboard`. It ran last, once I had the demo login, so it used the final code. The other 33 runs came before the last fixes listed below, so I ran one more full round of those cases on the final code, and **all 11 gave the expected result.** Cases 7 and 12 first stopped on my free API limit, so I reran those two after fixing how the tool waits. I don't count the first attempt at case 12, even though it showed ERROR as expected, because it stopped for the wrong reason.

### Sites it had never seen

I tuned the tool while watching those three sites, so that score is flattering. I then tried four new tests on three new sites, changing no code:

| Site | Test | Expected | Result |
|---|---|---|---|
| CloudQA practice form | Fill in a name, choose Female, verify it | PASS | PASS |
| books.toscrape.com | A book is listed in Travel | PASS | PASS |
| books.toscrape.com | Harry Potter is listed in Travel | FAIL | FAIL |
| demoblaze | Add a phone and find it in the cart | PASS | **FAIL** |

The demoblaze miss was a false failure: the AI judged the cart before its rows had loaded. I added the re-check described above. Two later runs passed, but both loaded in time, so they don't prove the re-check. A separate automated test does.

### Bugs found late

- **Page code counted as evidence.** The feature that catches vanishing messages also picked up the page's own script text, so a quote could have been "proven" by code no visitor can see. A test exposed this before it caused a wrong result that I know of. Script and style text is now excluded, and a test covers it.
- **Waiting too briefly after a per-minute limit.** The tool retried after a few seconds, which was too short for a free key to recover. It now waits 25 seconds.

### Automated tests

44 tests run without an API key, using real browser pages and a scripted stand-in for the AI. I also broke the code on purpose in ten ways, such as accepting any quote and turning off password removal, and confirmed the tests caught each one.

## Limitations

- **False failures still happen.** The hotel search reported FAIL in 1 of 3 runs, because the AI noticed a required Nationality field but stopped instead of filling it in. Unlike a false PASS, it's visible in the report, but it would wrongly blame the website.
- **Results vary between runs,** because AI models aren't deterministic. Run a test more than once before trusting one surprising result.
- **The results came from the free lite model.** The free daily limit on `gemini-3.5-flash` ran out during development, so nearly every run above used `gemini-3.5-flash-lite`. A stronger model may do better on complex pages, but I haven't measured that.
- **Evidence proves that text appeared, not what it means.** "Female" also appears as a form label whether or not it's selected, and the cart test's evidence was just "1". The AI still judges whether a quote supports the check, and short quotes are weak proof.
- **A free key has low limits.** I used up the daily quota on two of the three models during testing. Long runs on a free key can stop with ERROR, and the per-minute wait makes them slower.
- **CAPTCHAs aren't solved.** The tool stops with ERROR.
- **Canvas-only pages are unsupported,** because actions need a real page element. The AI can see a screenshot, but it can't click coordinates.
- **Text that flashes up and disappears** is only caught when it's added to the page. A message that was already there and just gets shown isn't.
- **The backup provider is slow and lightly tested.** The tool supports any OpenAI-compatible service. Qwen through BharatCode's free queue completed one real test correctly, a 2-step dropdown test, but took about 18 minutes for 3 AI calls. My first attempts gave up too early, so I added streaming to wait in the queue. At that speed it's a backup, not a practical main model, and I've only run that one test on it.
- **The site-specific tests depend on live demo sites,** which can change or go down.

## What I'd do next

- Compare models on the same suite, to see whether a stronger model removes the false failures
- Have a second, independent AI call judge the final page, to strengthen weak quotes
- Let a test case state its own expected evidence, so results rely less on AI judgement
