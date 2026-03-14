# SessionKeeper

**Never solve the same CAPTCHA twice.**

SessionKeeper manages browser sessions for automation. When your headless browser hits a login wall or CAPTCHA, it opens a visible browser for human intervention, saves the session, and returns to headless mode.

## The Problem

Your automation works perfectly — until it hits a login page with a CAPTCHA. You either pay for CAPTCHA solving services, or manually intervene every single run.

SessionKeeper fixes this: **log in once by hand, automate forever** (until the session actually expires).

## Install

```bash
pip install playwright && playwright install firefox
```

## Quick Start

```python
from sessionkeeper import SessionKeeper

async with SessionKeeper("reddit") as sk:
    page = await sk.get_authenticated_page("https://reddit.com")
    # Already logged in — do your automation
    await page.goto("https://reddit.com/r/blender/submit")
```

First run: a browser window opens → you log in normally → SessionKeeper saves the session.
Every subsequent run: headless, no CAPTCHA, no browser window.
When the session expires: the browser opens again. One login, and you're good.

## CLI

```bash
# Authenticate with a site
python sessionkeeper.py auth reddit

# Check session validity
python sessionkeeper.py check reddit

# Show all saved sessions
python sessionkeeper.py status

# Clear a session
python sessionkeeper.py clear reddit
```

## Built-in Sites

| Site | Login URL | Detection |
|------|-----------|-----------|
| Reddit | reddit.com/login | User menu elements |
| Gumroad | app.gumroad.com/login | Dashboard access |
| DEV.to | dev.to/enter | Dashboard redirect |
| Twitter/X | twitter.com/i/flow/login | Compose button |
| note.com | note.com/login | Dashboard access |

## Custom Sites

```python
config = {
    "login_url": "https://mysite.com/login",
    "check_url": "https://mysite.com/dashboard",
    "success_indicator": ".user-avatar, a[href*='settings']",
    "failure_indicator": "input[type='password']",
    "display_name": "My Site",
}

async with SessionKeeper("mysite", config=config) as sk:
    page = await sk.get_authenticated_page("https://mysite.com/dashboard")
```

## How It Works

1. Check for saved session (`~/.sessionkeeper/`)
2. Load session into headless browser → verify auth via CSS selectors
3. If valid → return authenticated page
4. If expired → open **visible** browser → wait for human login
5. On success → save `storage_state` → close visible browser → return headless page

Built on [Playwright](https://playwright.dev/python/). Sessions include cookies, localStorage, and sessionStorage.

## Why Not CAPTCHA Solving Services?

- **Cost**: $2-3/1000 solves adds up. SessionKeeper = 30 seconds per session cycle.
- **Reliability**: Services have 85-95% solve rates. Humans have 100%.
- **Breakage**: New CAPTCHA types break services. Humans adapt instantly.

## License

MIT
