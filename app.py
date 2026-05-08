"""
Arabic Personalized Newsletter SaaS
- Stripe subscriptions with 7-day free trial
- Claude Batch API (50% cheaper)
- Staggered sends (respect rate limits)
- 50 SAR/month (~$13.30 USD)
"""

from flask import Flask, request, jsonify, render_template, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import feedparser
import requests
import stripe
import json
import os
import re
import sqlite3
import time
import atexit
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Keys ────────────────────────────────
ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.getenv("STRIPE_PRICE_ID", "")
ADMIN_PASSWORD         = os.getenv("ADMIN_PASSWORD", "admin123")
APP_URL                = os.getenv("APP_URL", "http://localhost:5050")

stripe.api_key = STRIPE_SECRET_KEY

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "subscribers.db")

SENDS_PER_MINUTE = 4
DELAY_BETWEEN    = 60 / SENDS_PER_MINUTE  # 15 sec between emails

# ─────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                topics TEXT NOT NULL,
                sources TEXT NOT NULL,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                subscription_status TEXT DEFAULT 'trialing',
                trial_end TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_sent TEXT,
                newsletters_sent INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id INTEGER,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                subject TEXT,
                status TEXT,
                articles_count INTEGER,
                cost_usd REAL
            )
        """)
        conn.commit()

init_db()


# ─────────────────────────────────────────
# PIPELINE — FETCH ARTICLES
# ─────────────────────────────────────────

def fetch_from_sources(sources, topics):
    articles = []
    for source_url in sources:
        source_url = source_url.strip()
        if not source_url:
            continue
        try:
            feed = feedparser.parse(source_url)
            if feed.entries:
                for entry in feed.entries[:4]:
                    summary = entry.get("summary", entry.get("description", ""))
                    summary = re.sub(r"<[^>]+>", "", summary)[:1000]
                    articles.append({
                        "source":  feed.feed.get("title", source_url),
                        "title":   entry.get("title", ""),
                        "link":    entry.get("link", ""),
                        "summary": summary,
                    })
            else:
                r = requests.get(source_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                text = re.sub(r"<[^>]+>", "", r.text)[:2000]
                articles.append({
                    "source":  source_url,
                    "title":   f"محتوى من {source_url}",
                    "link":    source_url,
                    "summary": text[:800],
                })
        except Exception as e:
            print(f"  ⚠️  Could not fetch {source_url}: {e}")
    return articles[:20]


# ─────────────────────────────────────────
# PIPELINE — BATCH API (50% cheaper)
# ─────────────────────────────────────────

def build_prompt(subscriber_name, topics, articles):
    articles_text = "\n\n".join([
        f"[{i+1}] SOURCE: {a['source']}\nTITLE: {a['title']}\nCONTENT: {a['summary']}\nURL: {a['link']}"
        for i, a in enumerate(articles)
    ])
    topics_str = "، ".join(topics)
    return f"""You are an expert Arabic newsletter editor creating a personalized weekly newsletter for {subscriber_name}.

Their topics of interest: {topics_str}

From the articles below, select the 5 most relevant. Then:
1. Write a warm personalized Arabic intro (60 words) addressing {subscriber_name} directly
2. For each article write a 150-200 word Arabic summary in clear Modern Standard Arabic (فصحى مبسطة), relevant to their interests, ending with one actionable insight
3. Write a short Arabic closing remark (30 words)

Return ONLY valid JSON, no markdown:
{{
  "intro": "...",
  "articles": [
    {{"title_ar": "...", "source": "...", "summary_ar": "...", "insight_ar": "...", "url": "..."}}
  ],
  "closing": "..."
}}

ARTICLES:
{articles_text}"""


def submit_batch(batch_requests):
    response = requests.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={"requests": batch_requests}
    )
    return response.json()


def poll_batch(batch_id, max_wait=600):
    for _ in range(max_wait // 10):
        time.sleep(10)
        r = requests.get(
            f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}
        )
        data = r.json()
        if data.get("processing_status") == "ended":
            return data
    raise TimeoutError(f"Batch {batch_id} did not complete in time")


def get_batch_results(batch_id):
    r = requests.get(
        f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        stream=True
    )
    results = {}
    for line in r.iter_lines():
        if line:
            item = json.loads(line)
            custom_id = item.get("custom_id")
            if item.get("result", {}).get("type") == "succeeded":
                raw = item["result"]["message"]["content"][0]["text"].strip()
                raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
                try:
                    results[custom_id] = json.loads(raw)
                except json.JSONDecodeError:
                    results[custom_id] = None
    return results


# ─────────────────────────────────────────
# PIPELINE — HTML BUILDER
# ─────────────────────────────────────────

def build_newsletter_html(subscriber_name, topics, newsletter_data):
    week_str   = datetime.now().strftime("%d %B %Y")
    topics_str = " · ".join(topics)
    articles_html = ""
    for i, article in enumerate(newsletter_data.get("articles", [])):
        divider = "<tr><td style='height:1px;background:#e8e0d0;padding:0'></td></tr>" if i > 0 else ""
        articles_html += f"""
        {divider}
        <tr><td style="padding:32px 0 0 0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="padding-bottom:8px;">
              <span style="background:#1a1a2e;color:#c8a96e;font-size:10px;font-weight:700;padding:3px 10px;border-radius:2px;letter-spacing:1px;font-family:Arial,sans-serif;">{article.get('source','').upper()}</span>
            </td></tr>
            <tr><td style="padding-bottom:12px;">
              <h2 style="margin:0;font-size:21px;font-weight:700;color:#1a1a2e;line-height:1.5;direction:rtl;font-family:Georgia,serif;">{article.get('title_ar','')}</h2>
            </td></tr>
            <tr><td style="padding-bottom:14px;">
              <p style="margin:0;font-size:15px;line-height:2;color:#3a3a4a;direction:rtl;font-family:Tahoma,Arial,sans-serif;">{article.get('summary_ar','')}</p>
            </td></tr>
            <tr><td style="background:#faf7f0;border-right:3px solid #c8a96e;padding:12px 16px;border-radius:3px;">
              <p style="margin:0;font-size:13px;color:#7a5c1e;direction:rtl;font-family:Tahoma,Arial,sans-serif;"><strong>💡 الفائدة:</strong> {article.get('insight_ar','')}</p>
            </td></tr>
            <tr><td style="padding-top:10px;">
              <a href="{article.get('url','#')}" style="color:#c8a96e;font-size:12px;text-decoration:none;font-family:Arial,sans-serif;">← اقرأ المصدر الأصلي</a>
            </td></tr>
          </table>
        </td></tr>"""

    return f"""<!DOCTYPE html><html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><title>نشرتك الأسبوعية</title></head>
<body style="margin:0;padding:0;background:#ede9e0;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ede9e0;padding:30px 0;">
<tr><td align="center"><table width="620" cellpadding="0" cellspacing="0" border="0" style="max-width:620px;width:100%;">
  <tr><td style="background:#1a1a2e;padding:36px 40px 28px;border-radius:8px 8px 0 0;text-align:center;">
    <p style="margin:0 0 4px;color:#c8a96e;font-size:10px;letter-spacing:3px;font-family:Arial,sans-serif;">PERSONALIZED · شخصي</p>
    <h1 style="margin:0 0 6px;color:#fff;font-size:36px;font-family:Georgia,serif;">نشرتك الأسبوعية</h1>
    <p style="margin:0 0 14px;color:#8888aa;font-size:13px;font-family:Tahoma,Arial,sans-serif;direction:rtl;">مُعدَّة لـ {subscriber_name} · {week_str}</p>
    <div style="display:inline-block;background:rgba(200,169,110,0.15);border:1px solid #c8a96e;border-radius:20px;padding:5px 16px;">
      <p style="margin:0;color:#c8a96e;font-size:11px;font-family:Tahoma,Arial,sans-serif;">{topics_str}</p>
    </div>
  </td></tr>
  <tr><td style="background:#c8a96e;padding:20px 40px;">
    <p style="margin:0;font-size:15px;line-height:1.9;color:#1a1a2e;direction:rtl;font-family:Tahoma,Arial,sans-serif;font-weight:500;">{newsletter_data.get('intro','')}</p>
  </td></tr>
  <tr><td style="background:#fff;padding:10px 40px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0">{articles_html}</table>
  </td></tr>
  <tr><td style="background:#1a1a2e;padding:24px 40px;border-radius:0 0 8px 8px;">
    <p style="margin:0;font-size:14px;color:#c8a96e;direction:rtl;text-align:center;font-family:Tahoma,Arial,sans-serif;">{newsletter_data.get('closing','')}</p>
  </td></tr>
  <tr><td style="padding:20px 40px;text-align:center;">
    <p style="margin:0;color:#aaa;font-size:11px;font-family:Arial,sans-serif;"><a href="*|UNSUB|*" style="color:#aaa;">إلغاء الاشتراك</a></p>
  </td></tr>
</table></td></tr></table></body></html>"""


# ─────────────────────────────────────────
# PIPELINE — EMAIL SEND
# ─────────────────────────────────────────

def send_email(to_email, to_name, subject, html):
    mc_key = os.getenv("MAILCHIMP_API_KEY", "")
    if not mc_key:
        print(f"  📧 [preview only] Would send to {to_email}")
        return {"status": "preview_only"}
    r = requests.post(
        "https://mandrillapp.com/api/1.0/messages/send",
        json={"key": mc_key, "message": {
            "html": html, "subject": subject,
            "from_email": os.getenv("FROM_EMAIL", "newsletter@yourdomain.com"),
            "from_name": "نشرتك الأسبوعية",
            "to": [{"email": to_email, "name": to_name, "type": "to"}],
        }}
    )
    return r.json()


# ─────────────────────────────────────────
# WEEKLY BATCH PIPELINE
# ─────────────────────────────────────────

def run_weekly_batch_pipeline():
    print(f"\n{'='*55}\n🗞️  Weekly Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*55}")

    with get_db() as conn:
        subs = conn.execute(
            "SELECT * FROM subscribers WHERE active=1 AND subscription_status IN ('active','trialing')"
        ).fetchall()

    if not subs:
        print("No active subscribers.")
        return 0

    print(f"📋 {len(subs)} active subscribers")

    sub_articles = {}
    for sub in subs:
        sources  = json.loads(sub["sources"])
        topics   = json.loads(sub["topics"])
        articles = fetch_from_sources(sources, topics)
        if articles:
            sub_articles[sub["id"]] = {"sub": dict(sub), "articles": articles, "topics": topics}
            print(f"  ✓ {sub['name']}: {len(articles)} articles")

    # Build batch
    batch_requests = [
        {
            "custom_id": str(sub_id),
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": build_prompt(
                    d["sub"]["name"], d["topics"], d["articles"]
                )}]
            }
        }
        for sub_id, d in sub_articles.items()
    ]

    print(f"\n🤖 Submitting batch of {len(batch_requests)} to Claude Batch API...")
    batch    = submit_batch(batch_requests)
    batch_id = batch.get("id")
    if not batch_id:
        print(f"❌ Batch failed: {batch}")
        return 0

    print(f"  Batch ID: {batch_id} — polling...")
    poll_batch(batch_id)
    results = get_batch_results(batch_id)
    print(f"  ✅ {len(results)} results received")

    week_str   = datetime.now().strftime("%d/%m/%Y")
    sent_count = 0

    print(f"\n📬 Sending (staggered {SENDS_PER_MINUTE}/min)...")
    for sub_id_str, newsletter_data in results.items():
        if not newsletter_data:
            continue
        sub_id   = int(sub_id_str)
        if sub_id not in sub_articles:
            continue
        d        = sub_articles[sub_id]
        sub      = d["sub"]
        html     = build_newsletter_html(sub["name"], d["topics"], newsletter_data)
        subject  = f"نشرتك الأسبوعية الشخصية · {week_str}"
        result   = send_email(sub["email"], sub["name"], subject, html)
        status   = "sent" if isinstance(result, list) else "preview_only"
        cost_usd = (3000 * 1.5 / 1_000_000) + (1500 * 7.5 / 1_000_000)

        with get_db() as conn:
            conn.execute("UPDATE subscribers SET last_sent=?, newsletters_sent=newsletters_sent+1 WHERE id=?",
                         (datetime.now().isoformat(), sub_id))
            conn.execute("INSERT INTO newsletter_log (subscriber_id, subject, status, articles_count, cost_usd) VALUES (?,?,?,?,?)",
                         (sub_id, subject, status, len(newsletter_data.get("articles", [])), cost_usd))
            conn.commit()

        sent_count += 1
        print(f"  ✉️  {sub['name']} ({sub['email']}) — {status}")
        if sent_count < len(results):
            time.sleep(DELAY_BETWEEN)

    print(f"\n✅ Done — {sent_count} newsletters sent!")
    return sent_count


# ─────────────────────────────────────────
# STRIPE ROUTES
# ─────────────────────────────────────────

@app.route("/api/create-checkout", methods=["POST"])
def create_checkout():
    data    = request.json
    name    = data.get("name", "").strip()
    email   = data.get("email", "").strip().lower()
    topics  = data.get("topics", [])
    sources = data.get("sources", [])

    if not all([name, email, topics, sources]):
        return jsonify({"error": "جميع الحقول مطلوبة"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer_email=email,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "name":    name,
                    "topics":  json.dumps(topics),
                    "sources": json.dumps(sources),
                }
            },
            metadata={"name": name, "email": email,
                      "topics": json.dumps(topics), "sources": json.dumps(sources)},
            success_url=f"{APP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/?cancelled=1",
            locale="ar",
        )
        return jsonify({"url": session.url})
    except stripe.error.StripeError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig     = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    etype = event["type"]
    obj   = event["data"]["object"]

    if etype == "checkout.session.completed":
        meta      = obj.get("metadata", {})
        name      = meta.get("name", "")
        email     = meta.get("email", obj.get("customer_email", ""))
        topics    = meta.get("topics", "[]")
        sources   = meta.get("sources", "[]")
        sub_id    = obj.get("subscription")
        cus_id    = obj.get("customer")
        trial_end = None
        if sub_id:
            sub = stripe.Subscription.retrieve(sub_id)
            if sub.get("trial_end"):
                trial_end = datetime.fromtimestamp(sub["trial_end"]).isoformat()
        with get_db() as conn:
            try:
                conn.execute("""INSERT INTO subscribers (name,email,topics,sources,stripe_customer_id,stripe_subscription_id,subscription_status,trial_end)
                    VALUES (?,?,?,?,?,?,'trialing',?)""",
                    (name, email, topics, sources, cus_id, sub_id, trial_end))
            except sqlite3.IntegrityError:
                conn.execute("""UPDATE subscribers SET stripe_customer_id=?,stripe_subscription_id=?,subscription_status='trialing',trial_end=? WHERE email=?""",
                    (cus_id, sub_id, trial_end, email))
            conn.commit()
        print(f"✅ New subscriber: {name} ({email})")

    elif etype == "customer.subscription.updated":
        with get_db() as conn:
            conn.execute("UPDATE subscribers SET subscription_status=? WHERE stripe_subscription_id=?",
                         (obj.get("status"), obj.get("id")))
            conn.commit()

    elif etype == "customer.subscription.deleted":
        with get_db() as conn:
            conn.execute("UPDATE subscribers SET active=0,subscription_status='cancelled' WHERE stripe_subscription_id=?",
                         (obj.get("id"),))
            conn.commit()

    return jsonify({"status": "ok"})


# ─────────────────────────────────────────
# PAGE ROUTES
# ─────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route("/success")
def success():
    return render_template("success.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")


# ─────────────────────────────────────────
# ADMIN API
# ─────────────────────────────────────────

def check_admin(req):
    return req.headers.get("X-Admin-Password") == ADMIN_PASSWORD

@app.route("/api/admin/subscribers")
def admin_subscribers():
    if not check_admin(request): return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM subscribers ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/stats")
def admin_stats():
    if not check_admin(request): return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        total      = conn.execute("SELECT COUNT(*) as c FROM subscribers").fetchone()["c"]
        active     = conn.execute("SELECT COUNT(*) as c FROM subscribers WHERE subscription_status='active'").fetchone()["c"]
        trialing   = conn.execute("SELECT COUNT(*) as c FROM subscribers WHERE subscription_status='trialing'").fetchone()["c"]
        total_sent = conn.execute("SELECT COUNT(*) as c FROM newsletter_log").fetchone()["c"]
        total_cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) as c FROM newsletter_log").fetchone()["c"]
        recent     = conn.execute("SELECT * FROM newsletter_log ORDER BY sent_at DESC LIMIT 20").fetchall()
    return jsonify({
        "total_subscribers": total,
        "active_paying": active,
        "in_trial": trialing,
        "total_sent": total_sent,
        "total_api_cost_usd": round(total_cost, 4),
        "mrr_usd": round(active * 13.30, 2),
        "mrr_sar": round(active * 50, 2),
        "recent_activity": [dict(r) for r in recent]
    })

@app.route("/api/admin/send-all", methods=["POST"])
def send_all():
    if not check_admin(request): return jsonify({"error": "Unauthorized"}), 401
    try:
        count = run_weekly_batch_pipeline()
        return jsonify({"success": True, "sent": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/send/<int:sub_id>", methods=["POST"])
def send_single(sub_id):
    if not check_admin(request): return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        sub = conn.execute("SELECT * FROM subscribers WHERE id=?", (sub_id,)).fetchone()
    if not sub: return jsonify({"error": "Not found"}), 404

    topics   = json.loads(sub["topics"])
    sources  = json.loads(sub["sources"])
    articles = fetch_from_sources(sources, topics)
    if not articles: return jsonify({"error": "Could not fetch articles"}), 400

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 4000,
              "messages": [{"role": "user", "content": build_prompt(sub["name"], topics, articles)}]}
    )
    raw = resp.json()["content"][0]["text"].strip()
    raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
    nd  = json.loads(raw)
    html    = build_newsletter_html(sub["name"], topics, nd)
    subject = f"نشرتك الأسبوعية الشخصية · {datetime.now().strftime('%d/%m/%Y')}"
    send_email(sub["email"], sub["name"], subject, html)

    with get_db() as conn:
        conn.execute("UPDATE subscribers SET last_sent=?, newsletters_sent=newsletters_sent+1 WHERE id=?",
                     (datetime.now().isoformat(), sub_id))
        conn.execute("INSERT INTO newsletter_log (subscriber_id,subject,status,articles_count,cost_usd) VALUES (?,?,?,?,?)",
                     (sub_id, subject, "sent", len(nd.get("articles", [])), 0.03))
        conn.commit()

    return jsonify({"success": True, "preview_html": html})


# ─────────────────────────────────────────
# SCHEDULER (runs inside the web process)
# ─────────────────────────────────────────

def _scheduled_send():
    print(f"\n{'='*50}\n🗞️  Weekly send triggered: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*50}")
    try:
        run_weekly_batch_pipeline()
    except Exception as e:
        print(f"❌ Scheduled send error: {e}")

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    _scheduled_send,
    CronTrigger(day_of_week="sun", hour=9, minute=0),
    id="weekly_newsletter",
    name="Weekly Newsletter Send",
    replace_existing=True,
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

if __name__ == "__main__":
    app.run(debug=True, port=5050)
