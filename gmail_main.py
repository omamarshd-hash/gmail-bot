from flask import Flask, request, jsonify
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import base64
import requests
import os
import re
import json
import threading
import time
from dotenv import load_dotenv

# =========================================
# LOAD ENV VARIABLES
# =========================================

load_dotenv()

GOVERNOR_URL = os.getenv("GOVERNOR_URL")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GMAIL_USER = os.getenv("GMAIL_USER", "me")

# =========================================
# DEBUG PRINTS
# =========================================

print("\n======================")
print("ENV VARIABLES LOADED")
print("======================")
print("GOVERNOR URL:", GOVERNOR_URL)
print("GMAIL USER:", GMAIL_USER)
print("GOOGLE CREDENTIALS:", "LOADED" if GOOGLE_CREDENTIALS_JSON else "MISSING")

# =========================================
# FLASK APP
# =========================================

app = Flask(__name__)

# =========================================
# IN-MEMORY PROCESSED EMAILS SET
# (resets on redeploy — fine for demo)
# =========================================

processed_emails = set()

# =========================================
# GMAIL AUTH — from env var, no file needed
# =========================================

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events"
]

def get_gmail_service():
    try:
        creds_data = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        return build("gmail", "v1", credentials=creds)

    except Exception as e:
        print("❌ Gmail auth error:", str(e))
        return None


# =========================================
# SEND EMAIL REPLY
# =========================================

def send_email(service, to, subject, message_text, thread_id=None):
    try:
        message = MIMEMultipart()
        message["to"] = to
        message["subject"] = subject
        message.attach(MIMEText(message_text, "plain"))

        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode()

        body = {"raw": raw_message}
        if thread_id:
            body["threadId"] = thread_id

        service.users().messages().send(
            userId="me",
            body=body
        ).execute()

        print("\n✅ EMAIL REPLY SENT TO:", to)

    except Exception as e:
        print("❌ Send email error:", str(e))


# =========================================
# EXTRACT EMAIL BODY
# =========================================

def get_email_body(payload):
    body = ""

    if "parts" in payload:
        for part in payload["parts"]:
            mime_type = part.get("mimeType")
            data = part.get("body", {}).get("data")
            if mime_type == "text/plain" and data:
                body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    else:
        data = payload.get("body", {}).get("data")
        if data:
            body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return body.strip()


# =========================================
# BLOCKED SENDERS
# =========================================

BLOCKED_SENDERS = [
    "no-reply", "noreply", "donotreply",
    "do-not-reply", "mailer-daemon",
    "notifications", "notification",
    "render.com", "onrender.com",
    "groq.com", "github.com",
    "google.com", "googleapis.com",
    "accounts.google", "meta.com",
    "facebook.com", "instagram.com",
    "support@", "admin@", "billing@",
    "security@", "alert@", "team@",
    "newsletter", "digest", "weekly",
    "automated", "system", "daemon"
]

BULK_KEYWORDS = [
    "unsubscribe", "list-unsubscribe", "mailing list",
    "this is an automated", "do not reply",
    "automatically generated", "auto-generated",
    "deployment", "build failed", "build successful",
    "your account", "verify your", "click here to"
]


# =========================================
# ASK GOVERNOR AI
# =========================================

def wake_governor():
    try:
        base_url = GOVERNOR_URL.replace("/process_message", "")
        requests.get(base_url, timeout=10)
        print("✅ Governor is awake")
    except:
        pass


def ask_governor(sender_email, email_body, user_email=None):
    try:
        payload = {
            "platform": "gmail",
            "user_id": sender_email,
            "message": email_body,
            "user_email": user_email or sender_email
        }

        response = requests.post(
            GOVERNOR_URL,
            json=payload,
            timeout=60
        )

        print("\n======================")
        print("🧠 GOVERNOR RESPONSE")
        print("======================")
        print("STATUS:", response.status_code)
        print("RAW:", response.text)

        data = response.json()
        return data.get("reply", "Thank you for your email. We will get back to you shortly.")

    except Exception as e:
        print("❌ Governor error:", str(e))
        return "Thank you for your email. We will get back to you shortly."


# =========================================
# PROCESS A SINGLE EMAIL
# =========================================

def process_email(service, message_id):
    try:
        if message_id in processed_emails:
            print(f"⏭️ Already processed: {message_id}")
            return

        full_message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full"
        ).execute()

        payload = full_message["payload"]
        headers = payload.get("headers", [])
        thread_id = full_message.get("threadId")

        subject = ""
        sender = ""

        for header in headers:
            name = header.get("name", "")
            value = header.get("value", "")
            if name == "Subject":
                subject = value
            if name == "From":
                sender = value

        print("\n======================")
        print("📨 NEW EMAIL")
        print("======================")
        print("FROM:", sender)
        print("SUBJECT:", subject)

        # Block no-reply senders
        sender_lower = sender.lower()
        if any(blocked in sender_lower for blocked in BLOCKED_SENDERS):
            print("🚫 SKIPPING NO-REPLY EMAIL")
            processed_emails.add(message_id)
            return

        # Get email body
        email_body = get_email_body(payload)
        print("BODY PREVIEW:", email_body[:200])

        # Block bulk emails
        if any(kw in email_body.lower() for kw in BULK_KEYWORDS):
            print("🚫 SKIPPING BULK EMAIL")
            processed_emails.add(message_id)
            return

        # Extract sender email
        email_match = re.search(r'<(.+?)>', sender)
        sender_email = email_match.group(1) if email_match else sender

        # Wake Governor first
        wake_governor()

        # Ask Governor AI — pass sender email so no need to ask
        reply_text = ask_governor(sender_email, email_body, sender_email)

        print("\n🤖 AI REPLY:")
        print(reply_text)

        # Send reply
        send_email(
            service=service,
            to=sender_email,
            subject=f"Re: {subject}",
            message_text=f"Hello,\n\n{reply_text}",
            thread_id=thread_id
        )

        # Mark as read
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()

        # Save as processed
        processed_emails.add(message_id)
        print(f"✅ Email {message_id} processed successfully")

    except Exception as e:
        print(f"❌ Process email error: {str(e)}")


# =========================================
# HOME ROUTE
# =========================================

@app.route("/")
def home():
    return "Gmail AI Bot Running"


# =========================================
# GMAIL PUSH NOTIFICATION WEBHOOK
# Google Pub/Sub sends notifications here
# when new emails arrive
# =========================================

@app.route("/gmail/webhook", methods=["POST"])
def gmail_webhook():
    try:
        data = request.get_json()

        print("\n======================")
        print("📩 GMAIL PUSH NOTIFICATION")
        print("======================")
        print(data)

        # Decode Pub/Sub message
        if "message" in data:
            pubsub_message = data["message"]
            decoded = base64.b64decode(
                pubsub_message["data"]
            ).decode("utf-8")
            notification = json.loads(decoded)

            print("NOTIFICATION:", notification)

            email_address = notification.get("emailAddress")
            history_id = notification.get("historyId")

            print("EMAIL:", email_address)
            print("HISTORY ID:", history_id)

            # Get new emails using history
            service = get_gmail_service()
            if not service:
                return "SERVICE_ERROR", 500

            # Fetch recent unread emails only
            from datetime import datetime, timedelta
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")

            results = service.users().messages().list(
                userId="me",
                labelIds=["INBOX"],
                q=f"is:unread after:{yesterday}"
            ).execute()

            messages = results.get("messages", [])
            print(f"📬 Found {len(messages)} unread emails")

            for msg in messages:
                process_email(service, msg["id"])

        return "EVENT_RECEIVED", 200

    except Exception as e:
        print("❌ Webhook error:", str(e))
        return "ERROR", 500


# =========================================
# MANUAL POLL ROUTE
# Call this to manually check for new emails
# Useful for testing without Pub/Sub setup
# =========================================

@app.route("/gmail/poll", methods=["GET", "POST"])
def gmail_poll():
    try:
        print("\n======================")
        print("📬 MANUAL GMAIL POLL")
        print("======================")

        service = get_gmail_service()
        if not service:
            return jsonify({"error": "Gmail auth failed"}), 500

        # Only fetch emails from last 24 hours
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")

        results = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            q=f"is:unread after:{yesterday}"
        ).execute()

        messages = results.get("messages", [])
        print(f"Found {len(messages)} unread emails")

        processed_count = 0
        for msg in messages:
            process_email(service, msg["id"])
            processed_count += 1

        return jsonify({
            "success": True,
            "emails_found": len(messages),
            "emails_processed": processed_count
        })

    except Exception as e:
        print("❌ Poll error:", str(e))
        return jsonify({"error": str(e)}), 500


# =========================================
# SETUP GMAIL PUSH NOTIFICATIONS
# Call this once after deployment to register
# your Render URL with Google Pub/Sub
# =========================================

@app.route("/gmail/setup_push", methods=["POST"])
def setup_push():
    try:
        service = get_gmail_service()
        if not service:
            return jsonify({"error": "Gmail auth failed"}), 500

        RENDER_URL = os.getenv("RENDER_URL", "")
        PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "")

        if not PUBSUB_TOPIC:
            return jsonify({"error": "PUBSUB_TOPIC env var missing"}), 400

        # Register webhook with Gmail
        result = service.users().watch(
            userId="me",
            body={
                "labelIds": ["INBOX"],
                "topicName": PUBSUB_TOPIC
            }
        ).execute()

        print("✅ Gmail push notifications set up:", result)

        return jsonify({
            "success": True,
            "result": result
        })

    except Exception as e:
        print("❌ Setup push error:", str(e))
        return jsonify({"error": str(e)}), 500


# =========================================
# AUTO POLL SCHEDULER
# Automatically checks for new emails every 5 minutes
# =========================================

def auto_poll():
    print("✅ Auto-poll scheduler started")
    while True:
        time.sleep(300)  # every 5 minutes
        try:
            print("\n⏰ AUTO POLL TRIGGERED")
            service = get_gmail_service()
            if not service:
                print("❌ Auto-poll: Gmail auth failed")
                continue

            from datetime import datetime, timedelta
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")

            results = service.users().messages().list(
                userId="me",
                labelIds=["INBOX"],
                q=f"is:unread after:{yesterday}"
            ).execute()

            messages = results.get("messages", [])
            print(f"📬 Auto-poll found {len(messages)} unread emails")

            for msg in messages:
                process_email(service, msg["id"])

        except Exception as e:
            print(f"❌ Auto-poll error: {str(e)}")


# =========================================
# START SERVER
# =========================================

# Start auto-poll thread
poll_thread = threading.Thread(target=auto_poll, daemon=True)
poll_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
