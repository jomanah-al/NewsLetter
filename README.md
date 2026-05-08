# نشرتك — Personalized Arabic Newsletter SaaS

A full-stack platform that lets subscribers choose their own sources and topics,
then automatically generates and sends them a personalized Arabic newsletter every week using Claude AI.

---

## Project Structure

```
arabic_newsletter/
├── app.py              ← Flask backend (API + pipeline)
├── scheduler.py        ← Weekly auto-send (runs separately)
├── .env.example        ← Copy to .env and fill in keys
├── templates/
│   ├── index.html      ← Subscriber signup page
│   └── admin.html      ← Admin dashboard
└── data/
    ├── subscribers.db  ← Auto-created SQLite database
    └── previews/       ← HTML previews of sent newsletters
```

---

## Setup (15 minutes)

### 1. Install dependencies
```bash
pip install flask feedparser requests python-dotenv apscheduler
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and add your keys
```

**Keys you need:**
| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `MAILCHIMP_API_KEY` | Mailchimp → Account → Extras → API Keys (use Mandrill/Transactional) |
| `ADMIN_PASSWORD` | Choose any secure password |
| `FROM_EMAIL` | Your verified sender email in Mailchimp |

### 3. Run the app
```bash
python app.py
```
App runs at: http://localhost:5050

### 4. Start the weekly scheduler (separate terminal)
```bash
python scheduler.py
```

---

## How it works

1. **Subscriber signs up** at `http://localhost:5050`
   - Enters name, email, topics of interest
   - Adds their preferred RSS feeds or website URLs

2. **Every Sunday at 9AM**, the scheduler calls the send-all endpoint

3. **For each subscriber**, the pipeline:
   - Fetches articles from their specified sources (RSS/web scraping)
   - Sends articles + their topics to Claude API
   - Claude selects best articles, summarizes & translates to Arabic
   - Builds a beautiful RTL HTML email
   - Sends via Mailchimp Transactional (Mandrill)

4. **Admin dashboard** at `http://localhost:5050/admin`
   - View all subscribers
   - Manually trigger sends
   - Preview generated newsletters
   - View activity log

---

## Deploying to Production

### Option A: Railway (easiest, ~$5/month)
```bash
# Install Railway CLI
npm install -g @railway/cli
railway login
railway new
railway up
```

### Option B: DigitalOcean App Platform
- Connect your GitHub repo
- Set environment variables in dashboard
- Deploys automatically

### Option C: VPS (cheapest long-term)
```bash
# On your server
git clone your-repo
pip install -r requirements.txt
# Use gunicorn + nginx + supervisor
gunicorn app:app --bind 0.0.0.0:5050
```

---

## Monetization Ideas

- **Free tier**: 1 source, 3 topics
- **Pro ($9/month)**: Unlimited sources, priority sending
- **Team ($29/month)**: Multiple users, custom branding

---

## Notes on Sources

- **RSS feeds work best** — most major news sites offer them
- Direct URLs also work — the app will try to scrape them
- Some sites (Bloomberg, WSJ) block scrapers — use their RSS feeds instead
- Recommend suggesting these RSS feeds to users:
  - `https://feeds.bloomberg.com/wealth/news.rss`
  - `https://www.investopedia.com/feedbuilder/feed/getfeed/?feedName=rss_headline`
  - `https://www.nerdwallet.com/blog/feed/`
  - `https://rss.nytimes.com/services/xml/rss/nyt/PersonalFinance.xml`
