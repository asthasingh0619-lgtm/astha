from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pywebpush import webpush, WebPushException
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime
import pytz
import sqlite3
import json
import os
import uuid

# -----------------------
# App setup
# -----------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# Mount static folder
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

VAPID_PUBLIC_KEY = "BDKhTIxI05AlXXk_zbJxESluEqbGXe25m6k5BuIXHWHQhS4Eh58JajT7IGdR1jwa9bjPZLD_LxM58vrNIiHEaS8"
VAPID_PRIVATE_KEY = "nBBu_wCGpRaX_RZ0Te0RrygMUNQT5AhuQ25MnHP10_I"

# -----------------------
# Database setup
# -----------------------
DB_FILE = "notifications.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT UNIQUE,
    p256dh TEXT,
    auth TEXT,
    subscribed_at TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS scheduled_notifications (
    id TEXT PRIMARY KEY,
    title TEXT,
    message TEXT,
    url TEXT,
    run_time TIMESTAMP
)
""")
conn.commit()

# -----------------------
# APScheduler setup
# -----------------------
jobstores = {"default": SQLAlchemyJobStore(url="sqlite:///jobs.sqlite")}
executors = {"default": ThreadPoolExecutor(10)}
scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, timezone=pytz.UTC)
scheduler.start()

# -----------------------
# Helper functions
# -----------------------
def get_subscribers():
    cursor.execute("SELECT endpoint, p256dh, auth, subscribed_at FROM subscribers")
    rows = cursor.fetchall()
    subs = []
    for r in rows:
        subs.append({
            "endpoint": r[0],
            "keys": {"p256dh": r[1], "auth": r[2]},
            "subscribed_at": datetime.fromisoformat(r[3])
        })
    return subs

def send_notification_task(title, message, url=None, job_id=None):
    subs = get_subscribers()
    dead_subs = []

    host = os.environ.get("HOST", "http://localhost:8000")
    absolute_url = url if url else host
    icon_url = f"{host}/static/ima1.png"

    for sub in subs:
        try:
            payload = json.dumps({
                "title": title,
                "body": message,
                "url": absolute_url,
                "icon": icon_url
            })
            endpoint = sub["endpoint"]
            aud = endpoint.split("/")[2]
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": "mailto:test@test.com", "aud": f"https://{aud}"}
            )
        except WebPushException as ex:
            print("Push failed:", ex)
            if ex.response and ex.response.status_code == 410:
                dead_subs.append(sub["endpoint"])

    for ep in dead_subs:
        cursor.execute("DELETE FROM subscribers WHERE endpoint=?", (ep,))
    conn.commit()

# -----------------------
# Admin page
# -----------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request, "public_key": VAPID_PUBLIC_KEY})

# -----------------------
# Subscribe endpoint
# -----------------------
@app.post("/subscribe")
async def subscribe(subscription: dict):
    now = datetime.utcnow().isoformat()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO subscribers (endpoint, p256dh, auth, subscribed_at) VALUES (?, ?, ?, ?)",
            (
                subscription["endpoint"],
                subscription["keys"]["p256dh"],
                subscription["keys"]["auth"],
                now
            )
        )
        conn.commit()
        return {"message": "Subscribed"}
    except Exception as e:
        return {"error": str(e)}

# -----------------------
# Send notification
# -----------------------
@app.post("/send-notification")
async def send_notification(
    title: str = Form(...),
    message: str = Form(...),
    url: Optional[str] = Form(None),
    send_at: Optional[str] = Form(None)
):
    if send_at and send_at.strip():
        try:
            run_time = datetime.fromisoformat(send_at)
        except:
            return {"error": "Invalid datetime format"}
        ist = pytz.timezone("Asia/Kolkata")
        if run_time.tzinfo is None:
            run_time = ist.localize(run_time)
        utc_time = run_time.astimezone(pytz.UTC)

        job_id = str(uuid.uuid4())
        scheduler.add_job(
            send_notification_task,
            "date",
            run_date=utc_time,
            args=[title, message, url, job_id],
            id=job_id
        )

        cursor.execute(
            "INSERT INTO scheduled_notifications (id, title, message, url, run_time) VALUES (?, ?, ?, ?, ?)",
            (job_id, title, message, url, utc_time.isoformat())
        )
        conn.commit()
        return {"status": "Notification Scheduled", "id": job_id}

    send_notification_task(title, message, url)
    return {"status": "✅ Notification Sent"}

# -----------------------
# List notifications
# -----------------------
@app.get("/notifications")
async def list_notifications():
    cursor.execute("SELECT id, title, message, url, run_time FROM scheduled_notifications ORDER BY run_time DESC")
    rows = cursor.fetchall()
    result = []
    ist = pytz.timezone("Asia/Kolkata")
    for r in rows:
        run_time = datetime.fromisoformat(r[4]).astimezone(ist)
        sent = datetime.utcnow().replace(tzinfo=pytz.UTC) >= datetime.fromisoformat(r[4]).astimezone(pytz.UTC)
        result.append({
            "id": r[0],
            "title": r[1],
            "message": r[2],
            "url": r[3],
            "send_at": r[4],
            "sent": sent,
            "time": run_time.strftime("%Y-%m-%d %H:%M:%S")
        })
    return result

# -----------------------
# Delete notification
# -----------------------
@app.delete("/notifications/{job_id}")
async def delete_notification(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except:
        pass
    cursor.execute("DELETE FROM scheduled_notifications WHERE id=?", (job_id,))
    conn.commit()
    return {"status": "Deleted"}

# -----------------------
# Update notification
# -----------------------
@app.put("/notifications/{job_id}")
async def update_notification(
    job_id: str,
    title: str = Form(...),
    message: str = Form(...),
    send_at: str = Form(...),
    url: Optional[str] = Form(None)
):
    try:
        scheduler.remove_job(job_id)
    except:
        pass

    run_time = datetime.fromisoformat(send_at)
    ist = pytz.timezone("Asia/Kolkata")
    if run_time.tzinfo is None:
        run_time = ist.localize(run_time)
    utc_time = run_time.astimezone(pytz.UTC)

    cursor.execute(
        "UPDATE scheduled_notifications SET title=?, message=?, run_time=?, url=? WHERE id=?",
        (title, message, utc_time.isoformat(), url, job_id)
    )

    scheduler.add_job(
        send_notification_task,
        "date",
        run_date=utc_time,
        args=[title, message, url, job_id],
        id=job_id
    )

    conn.commit()
    return {"status": "Updated"}

# -----------------------
# Home
# -----------------------
@app.get("/")
def home():
    return {"status": "FastAPI running"}

# -----------------------
# Run uvicorn for Render
# -----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)