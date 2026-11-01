# 30. Sofascore through a captcha token that a browser obtains

Status: Accepted — 2026-10-14 (proposed 2026-10-13). Amends ADR 4 (scraping posture) and ADR 14 (access method) for Sofascore only.

## Context

### The challenge
ADR 3 and ADR 14 rest on a probe from 2026-10-11: Sofascore's `www.sofascore.com/api/v1/*` answered 200 to `curl_cffi` with Chrome impersonation. That no longer holds. Sofascore now answers API requests with `403 {"error": {"code": 403, "reason": "challenge"}}` from its own Varnish edge, and this is not a Cloudflare TLS block. Public reports date the challenge for plain clients to about June 2026 (ScraperFC #93). On our IP it has come and gone:

- 2026-10-13, 16:26–16:46 UTC: the #21 backfill fetched about 1,300 requests at about 0.9 requests per second. Then the challenge started.
- 19:37 UTC, after three idle hours: the first request was challenged. Every request since has been challenged too.
- 20:10 UTC: `curl_cffi` as `chrome` and as `safari`, a homepage warm-up with `Referer`/`Origin`, plain `curl` and `api.sofascore.com` were all challenged.
- 2026-10-14, 06:03 UTC: still challenged, about 13.5 hours later (`curl_cffi` as `chrome` and `safari`, plain `curl`).
- 2026-10-19: lifted. Plain `curl_cffi` requests answered 200, and the implementation launched no browser.

The homepage itself serves Sofascore's captcha page (`/captcha.html?redirectUrl=…`). It renders a Cloudflare Turnstile widget (sitekey `0x4AAAAAADcoqB6O4KpaoADv`). The solution is POSTed to `/api/v1/token/captcha` (`{"response": <turnstile token>, "provider": "turnstile"}`), which returns a JWT, the Captcha Token. The web app stores it in localStorage `sofa.captcha.token` and in the `sofa_captcha` cookie, and sends it as the `X-Captcha` request header.

### How the Captcha Token is bound
The Captcha Token is an HS256 JWT with three claims:
- `exp`: 60 minutes after it is issued.
- `ip`: the public IP that exchanged it.
- `f`: a 6-hex-digit fingerprint of **the client that exchanged it**, meaning the one that POSTed the Turnstile response to `/api/v1/token/captcha`. It is not a fingerprint of the browser that solved the widget.

The probes show the difference:

- **A token exchanged by a browser fails from `curl_cffi`.** This covers a hand-solved token, and one from a stealth browser. Both were rejected with `403 challenge`:
  - `curl_cffi` as `chrome`, `chrome146`, `chrome150` and `safari`, sent as `X-Captcha` or as the `sofa_captcha` cookie, with or without the browser's User-Agent;
  - plain `curl`.

  The browser that exchanged the token gets 200 for calls made from inside its own page.
- **A token that `curl_cffi` exchanges works for `curl_cffi`.** A browser solved the widget, and its POST to `/token/captcha` was intercepted and aborted, so the response stayed unused. `curl_cffi` (`impersonate="chrome"`) then POSTed the same `{"response", "provider": "turnstile"}` body itself and got 200 with a token. Plain `curl_cffi` requests carrying that token as `X-Captcha` (or as the cookie) answered **200**. It worked on macOS and, 3 times out of 3, in a Linux container.

  A Turnstile response is bound to the sitekey, not to the browser that produced it. So **a browser is only needed to produce a Turnstile response, about once an hour.** Every API request can stay on `curl_cffi`.
- **The `x-requested-with` header does not matter.** The web app sends `x-requested-with: sha256(str(unix_time // 1800))[:6]`, which rotates every 30 minutes. It changed nothing, with or without a token, and it is not the `f` claim.

### Which browsers pass the Turnstile (probed 2026-10-13/14)
Every run was made the same way, one at a time and 20–30 seconds apart, each with a fresh profile:

1. load `captcha.html`;
2. wait up to 45 seconds for the redirect or for the `/token/captcha` POST;
3. on macOS, call three API paths from inside the page.

**On the developer's Mac (macOS, arm64):**

| Option | Headless | Headed |
|---|---|---|
| **camoufox 0.5.8** (anti-detect Firefox), no solver | **5/5** (4–29 s) | 1/1 |
| seleniumbase 4.56 UC mode | 3/4 (16–20 s) | 1/1 |
| Scrapling 0.4.15 `StealthySession(solve_cloudflare=True)` on patchright Chromium (#206, tunjayoff) | 1/4 | 1/1 |
| patchright 1.63, no solver | 0/1 | 1/1 (14 s) |
| zendriver 0.17 / nodriver 0.50 with `verify_cf()` | 0/1 each | 1/1 each |
| Playwright 1.62, no solver | 0/1 | — |
| Botasaurus 4.0 (`bypass_cloudflare=True`) | 0/1 ("Cloudflare has detected us") | 0/1 |
| Installed Chrome with Scrapling (`real_chrome=True`) | 0/1 | 1/1 (16 s) |

**In a Linux container (Debian bookworm, arm64, Docker Desktop):**

| Option | Result |
|---|---|
| camoufox, headless | 0/3. WebGL reports `null` |
| camoufox, **headed under Xvfb 1920×1080** (default, `geoip=True`, `os="linux"` with `humanize`) | **3/3** (3–5 s) |
| patchright Chromium, headless | 0/1 |
| patchright Chromium, headed under Xvfb 1920×1080 | 1/1 (3 s) |
| seleniumbase UC | does not start on Linux arm64 |

Headless Linux has no WebGL. That is the signal Turnstile catches, so headed under a full-size virtual display is what passes. The image also had Noto, Liberation and DejaVu fonts, and `TZ=Europe/Madrid`; whether those matter was not isolated. Earlier Xvfb attempts never launched, because `xauth` was missing.

### Soaks
Both ran at 0.5 requests per second for 450 requests (about 15 minutes), over season lists, event lists, events, lineups, shotmaps and average positions:
- camoufox headless on macOS, with every call made from inside the page: **450/450** answered 200.
- In the Linux container, camoufox headed under Xvfb solved once, `curl_cffi` exchanged the response, and every call went through `curl_cffi`: **450/450** answered 200.

Neither soak saw the challenge come back.

### Prior art
- **tunjayoff/sofascore_scraper PR #206** ("Turnstile and request-layer hardening", closed unmerged when its owner ended the project). It ran a long-lived "bridge" browser (Scrapling `StealthySession` on patchright Chromium, persistent profile) and sent every API request as an in-page `fetch()`. From its soak runs:
  - Tokens last 3,600 seconds. After expiry the API answers a `jwt:time-out-of-bounds` 403.
  - It refreshed tokens 15 minutes before `exp`, with at most 3 refresh loads per token.
  - After a failed solve it waited 180, 180, 360, then 600 seconds.
  - It loaded `captcha.html` at most 8 times per hour, and gave each solve 15 seconds to redirect (passing solves redirected in 3.4–4.5 seconds).
  - It cleared the cookie before a re-solve, because `captcha.html` redirects without issuing a new token while a cookie is present.
- **tunjayoff/sofascore_scraper `main`.** Its released `BrowserBridge` is headless patchright Chromium through Scrapling `StealthySession`, and it sends API calls from inside the page. Its changelog says it "passes the Cloudflare Turnstile challenge on its own" and "runs headless on a desktop and on a display-less server alike". In our probes that setup passed 1/4 headless on macOS and 0/1 headless in Linux.
- **Botasaurus**, an anti-detect Chromium wrapper.
  - Sharone-12/sofascore-scraper (October 2026) uses it headless and fetches the API URLs as page navigations.
  - ScraperFC PR #94 (open since June 2026, unmerged) warms a Botasaurus session on the homepage, then issues in-page XHRs. ScraperFC's own Sofascore scraper (issue #93) has been broken since June.
- **Crawlora** sells a hosted Sofascore API. ADR 14 rules out paid scraping APIs.
- **probberechts/soccerdata.**
  - #835 (May 2025) only gets past TLS fingerprinting, with `tls-requests`, which is what our `curl_cffi` already does.
  - #955 (July 2026) added a seleniumbase captcha solver, but only for its Selenium readers (WhoScored, FBref).
  - Its Sofascore reader is still plain HTTP, so it has nothing for this challenge.
- **camoufox's own tracker.**
  - "Cloudflare detects camoufox when running in Docker" (#311, merged into the detection tracker #686, which is open): the same script and IP pass on the host and fail in Docker, and the maintainers narrow it to the container's fonts, rendering and display.
  - `headless="virtual"` started Xvfb with a 1×1 screen (#458).
  - #574 notes that Turnstile responses are bound to the sitekey, not to the browser that produced them. That is the observation this ADR builds on.
- **#34 / PR #35.** A challenge now raises `ChallengeError`. It is not retried, and a backfill stops at the first one instead of spending every Match's attempts against it.

## Decision
- **Sofascore requests stay on `curl_cffi`.** `CaptchaTransport`, between the source interface and `ImpersonatingTransport` (ADR 14), adds an `X-Captcha` header with the current Captcha Token. The worker keeps **one** impersonating session for all Sofascore requests, so whatever Sofascore pairs with the token (its `sofa_captcha` cookie, say) travels with it from one activity to the next. The rate limit, circuit breaker, Raw Response recording, Fetch Attempts, activities and parsers do not change.
- **A Token Provider supplies the Captcha Token.** It lives in the worker process and is shared by every Sofascore request.
  1. It launches **camoufox, headed, under Xvfb with a 1920×1080 screen**, with `geoip=True`, and loads `captcha.html`. camoufox passes the Turnstile without a solver. `geoip` sets the browser's timezone and locale from the exit IP. In the worker image, a UTC browser on a Spanish IP got the interactive checkbox instead, and failed. The same image with the timezone matching the IP passed.
  2. It intercepts the page's POST to `/api/v1/token/captcha`, aborts it, and closes the browser.
  3. It POSTs the Turnstile response from `curl_cffi`, through the same impersonation as every API request, so the token's `f` claim matches the client that will use it.
- **When it obtains a token** (`CaptchaTransport`, between the source interface and `ImpersonatingTransport`):
  - **Only when challenged.** A request goes with the current token, or without one when there is none. When it is answered `challenge` or `jwt:*`, the transport obtains a token and sends the request once more. A request without a token is therefore the plain probe: while Sofascore does not challenge, no browser is launched.
  - **Dropped near expiry.** A token is dropped 5 minutes before its `exp`, and the next request goes without one. It is not refreshed ahead of time: re-checking whether the challenge has lifted costs one request an hour.
  - **Shared.** Requests challenged at the same time share one attempt. When the token arrives they all resend at once, one per concurrent activity (three in a backfill), past the rate limit.
  - **Bounded.** An attempt, solve and exchange together, is cut off after 40 seconds. That keeps a request waiting on it inside the activities' one-minute heartbeat timeout.
  - **Backoff.** After a failed attempt it backs off 180, 180, 360, then 600 seconds, and it makes at most 8 attempts in any hour (#206's limits). While it is backing off, the challenged answer goes back to the caller, which raises `ChallengeError`, so a backfill stops as PR #35 does today.
  - **Not rate-limited or recorded.** The exchange and the resend bypass the rate limit, and are not recorded as Raw Responses: the exchange's answer is the token itself, and there are at most a few of each an hour. The recorded answer is the request's last one.
  - **Configuration.** `SOFASCORE__CAPTCHA_HEADLESS` (default false) runs the browser headless where that passes, as on macOS. The browser goes through `SOFASCORE__PROXY` when one is set.
- **Where it runs.** In the worker container. The image gains camoufox's Firefox (`camoufox fetch`, into a cache owned by the worker user, since camoufox writes there at run time), Xvfb (started by the container's command on display `:99`) and fonts. The browser runs for about 10 seconds an hour, so there is no long-lived page and no browser to recycle. ADR 22 keeps ingestion on the developer's machine. Moving it to a server stays possible, apart from the IP question below.
- **Fallback.** If camoufox stops passing, the next option is patchright Chromium headed under Xvfb (1/1 in the container). Every headed option passed on macOS.
- **The token is a secret.** It is held in memory as a `SecretStr`, and it never appears in logs, Raw Responses, Fetch Attempts or reprs. camoufox and Playwright are imported only by the Token Provider, which is enforced by an import-linter contract.
- **Unchanged:**
  - Sofascore's rate limit stays at 0.5 requests per second (ADR 4).
  - No scraped data is committed or redistributed.
  - Transfermarkt and Understat do not change.
- **Amendments:**
  - ADR 4 gains an exception: for Sofascore, the platform passes the source's human-verification challenge with an anti-detect browser, at polite rates.
  - ADR 14 gains: Sofascore's `curl_cffi` requests carry a Captcha Token that an anti-detect browser (camoufox) obtains about once an hour.

## Consequences
- **Sofascore stays the primary source of stats and spatial data (ADR 3).** The request path barely changes: one header.
- **The worker image grows** by camoufox's Firefox, Xvfb and fonts, a few hundred MB. A browser runs for seconds about once an hour, not continuously.
- **New pinned dependency:** `camoufox`, plus the Firefox build it fetches. A change to Turnstile, to camoufox's fingerprints, or to how Sofascore binds tokens can break this without warning. The failure is loud (`ChallengeError`). The fix is a version bump or the fallback, and #206's pinned-version tests are the model for catching it.
- **A cheap way back.** If Sofascore lifts the challenge, the Token Provider's plain probe sees a 200 and stops launching the browser. If Sofascore binds the token to the solving browser again, the decision falls back to sending every call from inside the browser's page. That was verified on macOS (450/450) but not in Linux.
- **Terms of use.** Passing a site's bot check with an anti-detect browser very likely breaches Sofascore's terms of use, more directly than impersonating a browser's TLS, even though nothing clicks the checkbox. That is the main cost of this decision and the reason it needs explicit acceptance. If it is not accepted, the alternatives are:
  - ingest Sofascore only while the challenge happens to lift, as on 2026-10-13 16:26–16:46 UTC;
  - drop Sofascore and take spatial data from another source under a new ADR 3 decision.
- **Tests.** Tests keep stubbing the transport. The Token Provider's logic (refresh timing, backoff, the hourly load cap, the plain probe, held-back requests) is unit-tested with a fake browser and a fake exchange.

## Verified with the implementation (2026-10-14)
- **On macOS**, the real Token Provider over `ImpersonatingTransport` obtained a token on the first challenged request in about 20 seconds. Three API calls answered 200.
- **In the worker image**, it obtained a token 3/3, in 7–10 seconds.
- **Through the worker**, `fdap ingest-match 16363244`, a Match challenged on 2026-10-13, stored 66 Raw Responses, all 200, with one token obtained.
- **No leaks.** No Raw Response, Fetch Attempt or log line contains the token.

### Low-reputation IPs (observed 2026-10-19)
With a VPN accidentally on (NordVPN, a Brazilian exit), camoufox with `geoip` correctly took the São Paulo timezone and pt-BR. Turnstile still showed the interactive "Confirme que é humano" checkbox, headed and headless. camoufox does not click, so every attempt timed out. With the VPN off, on the home IP, the same code passed again.

So passing without a click depends on IP reputation, not only on the browser. A datacenter or VPN IP, which is what a cloud server has, gets the checkbox. This is the strongest argument for open question 3.

## Open questions
1. Does a session hold across token expiry and over several hours? Both soaks ran for 15 minutes, and tokens last 60. A soak of several hours answers it, and also whether volume brings the challenge back with a valid token.
2. How often does obtaining a token fail over a day? #206's soak saw three failed refreshes in a row and one 24-minute outage.
3. A server's IP. ADR 22 records that datacenter IP ranges are reportedly blocked, and the token's `ip` claim binds it to the exchanging IP. A VPN exit already got the interactive checkbox (above), so moving ingestion off the developer's machine needs one of two things, and a new ADR (ADR 22):
   - a sticky residential proxy, shared by the browser and `curl_cffi`, whose exit IP holds for at least an hour;
   - a solver that clicks the checkbox.
4. Do the fonts matter in the container, or only headed Xvfb and the matching timezone? This only affects the image size (4.5 GB).
5. Is the `X-Captcha` header alone enough, or does Sofascore also want the `sofa_captcha` cookie the exchange sets? It could not be tested on 2026-10-19 while the challenge was lifted. The shared session makes it moot.
