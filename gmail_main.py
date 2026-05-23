from flask import Flask, request, jsonify
from groq import Groq
import os
import json
import re
import threading
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import requests
import pickle

# =========================================
# LOAD ENV VARIABLES
# =========================================

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# =========================================
# DEBUG PRINTS
# =========================================

print("\n======================")
print("ENV VARIABLES LOADED")
print("======================")
print("GROQ API KEY:", GROQ_API_KEY[:25] if GROQ_API_KEY else "MISSING")
print("DATABASE URL:", DATABASE_URL[:40] if DATABASE_URL else "MISSING")

# =========================================
# FLASK APP
# =========================================

app = Flask(__name__)

# =========================================
# GROQ CLIENT
# =========================================

groq_client = Groq(api_key=GROQ_API_KEY)

# =========================================
# DATABASE CONNECTION
# =========================================

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


# =========================================
# INITIALIZE DATABASE SCHEMA
# =========================================

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Conversations table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            platform VARCHAR(50),
            user_id VARCHAR(255),
            role VARCHAR(20),
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Meetings table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id SERIAL PRIMARY KEY,
            platform VARCHAR(50),
            user_id VARCHAR(255),
            title VARCHAR(255),
            meeting_date VARCHAR(100),
            meeting_time VARCHAR(100),
            description TEXT,
            google_event_id VARCHAR(255),
            google_meet_link VARCHAR(500),
            user_email VARCHAR(255),
            status VARCHAR(50) DEFAULT 'scheduled',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # User profiles — stores email collected from Instagram etc
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id SERIAL PRIMARY KEY,
            platform VARCHAR(50),
            user_id VARCHAR(255),
            email VARCHAR(255),
            UNIQUE(platform, user_id)
        )
    """)

    # Pending meetings — waiting for user email before finalizing
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_meetings (
            id SERIAL PRIMARY KEY,
            platform VARCHAR(50),
            user_id VARCHAR(255),
            title VARCHAR(255),
            meeting_date VARCHAR(100),
            meeting_time VARCHAR(100),
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Logs table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            event_type VARCHAR(100),
            platform VARCHAR(50),
            user_id VARCHAR(255),
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("\n✅ DATABASE SCHEMA INITIALIZED")


# =========================================
# DB HELPERS
# =========================================

def save_message(platform, user_id, role, message):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO conversations (platform, user_id, role, message) VALUES (%s, %s, %s, %s)",
            (platform, user_id, role, message)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ DB save_message error:", str(e))


def get_conversation_history(platform, user_id, limit=10):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT role, message FROM conversations
               WHERE platform=%s AND user_id=%s
               ORDER BY timestamp DESC LIMIT %s""",
            (platform, user_id, limit)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        # Reverse so oldest is first
        return list(reversed(rows))
    except Exception as e:
        print("❌ DB get_history error:", str(e))
        return []


def save_meeting(platform, user_id, title, meeting_date, meeting_time, description, google_event_id="", google_meet_link=""):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO meetings
               (platform, user_id, title, meeting_date, meeting_time, description, google_event_id, google_meet_link)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (platform, user_id, title, meeting_date, meeting_time, description, google_event_id, google_meet_link)
        )
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Meeting saved to DB")
    except Exception as e:
        print("❌ DB save_meeting error:", str(e))


def cancel_meeting_in_db(platform, user_id, date_str):
    """Cancel a meeting by date and return the google_event_id if found"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, google_event_id FROM meetings
               WHERE platform=%s AND user_id=%s AND meeting_date=%s AND status='scheduled'
               ORDER BY created_at DESC LIMIT 1""",
            (platform, user_id, date_str)
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE meetings SET status='cancelled' WHERE id=%s",
                (row["id"],)
            )
            conn.commit()
            cur.close()
            conn.close()
            print(f"✅ Meeting cancelled in DB: id={row['id']}")
            return row["google_event_id"]
        cur.close()
        conn.close()
        return None
    except Exception as e:
        print("❌ DB cancel_meeting error:", str(e))
        return None


def get_upcoming_meetings(platform, user_id):
    """Get all scheduled meetings for a user"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT title, meeting_date, meeting_time FROM meetings
               WHERE platform=%s AND user_id=%s AND status='scheduled'
               ORDER BY meeting_date ASC""",
            (platform, user_id)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print("❌ DB get_upcoming_meetings error:", str(e))
        return []


def save_user_email(platform, user_id, email):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (platform, user_id, email)
            VALUES (%s, %s, %s)
            ON CONFLICT (platform, user_id) DO UPDATE SET email=%s
        """, (platform, user_id, email, email))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ User email saved: {email}")
    except Exception as e:
        print("❌ DB save_user_email error:", str(e))


def get_user_email(platform, user_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT email FROM user_profiles WHERE platform=%s AND user_id=%s",
            (platform, user_id)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row["email"] if row else None
    except Exception as e:
        print("❌ DB get_user_email error:", str(e))
        return None


def save_pending_meeting(platform, user_id, title, date, time, description):
    try:
        conn = get_db()
        cur = conn.cursor()
        # Remove any existing pending meeting for this user
        cur.execute(
            "DELETE FROM pending_meetings WHERE platform=%s AND user_id=%s",
            (platform, user_id)
        )
        cur.execute("""
            INSERT INTO pending_meetings (platform, user_id, title, meeting_date, meeting_time, description)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (platform, user_id, title, date, time, description))
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Pending meeting saved")
    except Exception as e:
        print("❌ DB save_pending_meeting error:", str(e))


def get_pending_meeting(platform, user_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM pending_meetings WHERE platform=%s AND user_id=%s ORDER BY created_at DESC LIMIT 1",
            (platform, user_id)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print("❌ DB get_pending_meeting error:", str(e))
        return None


def delete_pending_meeting(platform, user_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM pending_meetings WHERE platform=%s AND user_id=%s",
            (platform, user_id)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ DB delete_pending_meeting error:", str(e))


def save_log(event_type, platform, user_id, details):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO logs (event_type, platform, user_id, details) VALUES (%s, %s, %s, %s)",
            (event_type, platform, user_id, details)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ DB save_log error:", str(e))


# =========================================
# GOOGLE CALENDAR
# =========================================

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():
    try:
        creds = None

        if GOOGLE_CREDENTIALS_JSON:
            creds_data = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("⚠️ Google Calendar: no valid credentials")
                return None

        service = build("calendar", "v3", credentials=creds)
        return service

    except Exception as e:
        print("❌ Google Calendar service error:", str(e))
        return None


def send_email_notification(to_email, subject, body):
    """Send email notification via Gmail API"""
    try:
        service = get_gmail_service_for_email()
        if not service:
            print("⚠️ Email notification skipped — no Gmail service")
            return False

        from email.mime.text import MIMEText
        import base64

        message = MIMEText(body)
        message["to"] = to_email
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode()

        service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        print(f"✅ Email notification sent to {to_email}")
        return True

    except Exception as e:
        print(f"❌ Email notification error: {str(e)}")
        return False


def get_gmail_service_for_email():
    """Get Gmail service using same credentials"""
    try:
        creds_data = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events"
        ]
        creds = Credentials.from_authorized_user_info(creds_data, scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        print("❌ Gmail service error:", str(e))
        return None


def cancel_google_event(event_id):
    """Delete a Google Calendar event by ID"""
    try:
        if not event_id:
            return
        service = get_calendar_service()
        if not service:
            return
        service.events().delete(
            calendarId="primary",
            eventId=event_id
        ).execute()
        print(f"✅ Google Calendar event deleted: {event_id}")
    except Exception as e:
        print("❌ Google Calendar delete error:", str(e))


def check_calendar_availability(date_str, time_str):
    """Returns True if slot is free, False if busy"""
    try:
        service = get_calendar_service()
        if not service:
            return True  # assume free if no calendar access

        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=1)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=start_dt.isoformat() + "+05:00",
            timeMax=end_dt.isoformat() + "+05:00",
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = events_result.get("items", [])

        if events:
            print(f"⚠️ Slot busy — {len(events)} event(s) found at {date_str} {time_str}")
            return False

        print(f"✅ Slot is free at {date_str} {time_str}")
        return True

    except Exception as e:
        print("❌ Availability check error:", str(e))
        return True  # assume free on error


def create_calendar_event(title, date_str, time_str, description="", attendee_email=None):
    try:
        service = get_calendar_service()
        if not service:
            print("⚠️ Skipping Google Calendar — no credentials")
            return None, None

        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=1)

        event = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "Asia/Karachi"
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "Asia/Karachi"
            },
            "conferenceData": {
                "createRequest": {
                    "requestId": f"fyp-{start_dt.timestamp()}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"}
                }
            }
        }

        # Add attendee if email provided
        if attendee_email:
            event["attendees"] = [{"email": attendee_email}]
            event["guestsCanSeeOtherGuests"] = False

        created = service.events().insert(
            calendarId="primary",
            body=event,
            conferenceDataVersion=1,
            sendUpdates="all" if attendee_email else "none"
        ).execute()

        event_id = created.get("id", "")
        meet_link = created.get("hangoutLink", "")

        print(f"✅ Google Calendar event created: {event_id}")
        print(f"📅 Meet link: {meet_link}")

        return event_id, meet_link

    except Exception as e:
        print("❌ Google Calendar create event error:", str(e))
        return None, None


# =========================================
# CEO ASSISTANT SYSTEM PROMPT
# =========================================

def get_system_prompt(business_context=""):
    today = datetime.now()
    today_str = today.strftime("%A, %B %d, %Y")
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    current_year = today.strftime("%Y")

    return f"""You are an elite AI executive assistant managing all social media communications on behalf of a busy CEO.

TODAY'S DATE: {today_str}
CURRENT YEAR: {current_year}
TOMORROW: {tomorrow_str}

{f"BUSINESS CONTEXT:{business_context}" if business_context else ""}

YOUR PERSONA:
- Professional, warm, and highly efficient
- You represent the CEO with authority and grace
- You speak in first person on behalf of the CEO ("The CEO will..." or "We would be happy to...")
- You never mention you are an AI unless directly asked
- You keep replies concise — 2-4 sentences for simple queries, more only when needed

YOUR CAPABILITIES:
1. GENERAL INQUIRIES — Answer questions about the business, services, pricing, partnerships
2. MEETING SCHEDULING — Book meetings with proper date/time handling
3. MEETING CANCELLATION — Cancel existing meetings when requested
4. TASK NOTING — Acknowledge requests, complaints, or tasks professionally
5. COMPLAINTS — Handle with empathy, escalate professionally
6. FOLLOW-UPS — Acknowledge and confirm next steps clearly

RESPONSE GUIDELINES:
- Match the tone of the person messaging (formal if they're formal, friendly if casual)
- For complaints: empathize first, then offer a resolution path
- For inquiries you can't answer: say the CEO will follow up personally
- For tasks/requests: confirm you've noted it and will action it
- Always end interactions positively and professionally
- End every reply with exactly: "Best regards,\nExecutive Assistant" — no placeholders, no variations
- NEVER use "[CEO's Representative]" or any placeholder text in signatures

STRICT MEETING RULES:
- Only add [MEETING_REQUEST:...] when the user explicitly asks to SCHEDULE/BOOK a meeting
- Only add [CANCEL_MEETING:...] when the user explicitly asks to CANCEL a meeting
- NEVER add these blocks for thank you messages, greetings, complaints, or general questions
- A "thank you" or "ok" or "sure" is NEVER a meeting request or cancellation

MEETING SCHEDULING — only when explicitly requested:
[MEETING_REQUEST: {{"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "description": "..."}}]
- Only include when you have title + date + time
- Always use YYYY-MM-DD format, never relative terms
- Ask for missing details naturally if needed

MEETING CANCELLATION — only when explicitly requested:
[CANCEL_MEETING: {{"date": "YYYY-MM-DD"}}]
- Only include when cancellation is clearly and explicitly requested
- Convert relative dates (Sunday, Monday, tomorrow) to YYYY-MM-DD
- Keep reply short: confirm the cancellation simply
"""


# =========================================
# INTENT DETECTION + AI REPLY
# =========================================

def process_with_ai(platform, user_id, user_message, business_context=""):

    # Get conversation history
    history = get_conversation_history(platform, user_id, limit=10)

    # Get upcoming meetings for context
    upcoming = get_upcoming_meetings(platform, user_id)
    meetings_context = ""
    if upcoming:
        meetings_list = "\n".join([
            f"- {m['title']} on {m['meeting_date']} at {m['meeting_time']}"
            for m in upcoming
        ])
        meetings_context = f"\n\nSCHEDULED MEETINGS FOR THIS USER:\n{meetings_list}"

    # Build system prompt with business context + meetings
    system_prompt = get_system_prompt(business_context) + meetings_context
    messages = [{"role": "system", "content": system_prompt}]

    # Add history
    for row in history:
        messages.append({
            "role": row["role"],
            "content": row["message"]
        })

    # Add current message
    messages.append({
        "role": "user",
        "content": user_message
    })

    print("\n======================")
    print("🤖 CALLING GROQ AI")
    print("======================")
    print(f"History loaded: {len(history)} messages")
    print(f"Upcoming meetings: {len(upcoming)}")

    # Call Groq
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=500,
        temperature=0.7
    )

    full_reply = response.choices[0].message.content.strip()

    print("RAW AI REPLY:", full_reply)

    # =========================================
    # DETECT MEETING REQUEST IN REPLY
    # =========================================

    meeting_data = None
    cancel_data = None
    clean_reply = full_reply

    # Check for cancellation
    cancel_match = re.search(
        r'\[CANCEL_MEETING:\s*(\{.*?\})\]',
        full_reply,
        re.DOTALL
    )

    if cancel_match:
        try:
            cancel_data = json.loads(cancel_match.group(1))
            clean_reply = full_reply[:cancel_match.start()].strip()
            print("\n🗑️ CANCELLATION DETECTED:", cancel_data)
        except Exception as e:
            print("❌ Cancel JSON parse error:", str(e))

    # Check for new meeting request (only if not a cancellation)
    if not cancel_data:
        meeting_match = re.search(
            r'\[MEETING_REQUEST:\s*(\{.*?\})\]',
            full_reply,
            re.DOTALL
        )
        if meeting_match:
            try:
                meeting_data = json.loads(meeting_match.group(1))
                clean_reply = full_reply[:meeting_match.start()].strip()
                print("\n📅 MEETING DETECTED:", meeting_data)
            except Exception as e:
                print("❌ Meeting JSON parse error:", str(e))

    return clean_reply, meeting_data, cancel_data


# =========================================
# HOME ROUTE
# =========================================

@app.route("/")
def home():
    return "Governor AI Running"


# =========================================
# MAIN PROCESS MESSAGE ENDPOINT
# =========================================

@app.route("/process_message", methods=["POST"])
def process_message():

    data = request.get_json()

    print("\n======================")
    print("📩 GOVERNOR RECEIVED")
    print("======================")
    print(data)

    platform = data.get("platform", "unknown")
    user_id = data.get("user_id", "unknown")
    user_message = data.get("message", "")
    business_context = data.get("business_context", "")
    user_email = data.get("user_email", "")  # provided by Gmail bot

    # Auto-save email if provided by platform (e.g. Gmail)
    if user_email and "@" in user_email:
        save_user_email(platform, user_id, user_email)
        print(f"✅ Auto-saved user email: {user_email}")

    if not user_message:
        return jsonify({"reply": "I didn't receive a message."})

    # Save user message to DB
    save_message(platform, user_id, "user", user_message)
    save_log("message_received", platform, user_id, user_message)

    try:
        # =========================================
        # IGNORE SOCIAL RESPONSES
        # Don't treat thank you/greetings as actions
        # =========================================

        social_phrases = [
            "thank you", "thanks", "perfect", "great",
            "awesome", "ok", "okay", "sure", "noted",
            "got it", "understood", "no problem", "welcome",
            "bye", "goodbye", "see you", "talk later",
            "sounds good", "alright", "fine", "good"
        ]

        message_lower = user_message.lower().strip()
        is_social = any(phrase in message_lower for phrase in social_phrases)

        # =========================================
        # CHECK IF USER IS PROVIDING THEIR EMAIL
        # (after bot asked for it)
        # =========================================

        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        email_match = re.search(email_pattern, user_message)

        pending = get_pending_meeting(platform, user_id)

        if email_match and pending and not is_social:
            # User just provided their email — finalize the meeting
            user_email = email_match.group(0)
            print(f"\n📧 EMAIL COLLECTED: {user_email}")

            # Save email to user profile
            save_user_email(platform, user_id, user_email)

            # Check availability
            is_free = check_calendar_availability(
                pending["meeting_date"], pending["meeting_time"]
            )

            if not is_free:
                reply = f"I'm sorry, the CEO is not available on {pending['meeting_date']} at {pending['meeting_time']}. Could you suggest another date and time?"
            else:
                # Create calendar event with attendee
                event_id, meet_link = create_calendar_event(
                    pending["title"],
                    pending["meeting_date"],
                    pending["meeting_time"],
                    pending["description"],
                    attendee_email=user_email
                )

                # Save meeting to DB
                save_meeting(
                    platform, user_id,
                    pending["title"],
                    pending["meeting_date"],
                    pending["meeting_time"],
                    pending["description"],
                    event_id or "",
                    meet_link or ""
                )

                # Send cancellation confirmation email
                send_email_notification(
                    to_email=user_email,
                    subject=f"Meeting Confirmed: {pending['title']}",
                    body=(
                        f"Hello,\n\n"
                        f"Your meeting has been confirmed.\n\n"
                        f"Details:\n"
                        f"Title: {pending['title']}\n"
                        f"Date: {pending['meeting_date']}\n"
                        f"Time: {pending['meeting_time']}\n"
                        f"Google Meet: {meet_link or 'Link will be shared shortly'}\n\n"
                        f"Best regards,\n"
                        f"Executive Assistant"
                    )
                )

                # Delete pending meeting
                delete_pending_meeting(platform, user_id)
                save_log("meeting_scheduled", platform, user_id,
                         f"{pending['title']} on {pending['meeting_date']} at {pending['meeting_time']}")

                reply = f"Your meeting has been confirmed for {pending['meeting_date']} at {pending['meeting_time']}."
                if meet_link:
                    reply += f"\n\nGoogle Meet link: {meet_link}"
                reply += f"\n\nA calendar invite has been sent to {user_email}."

            save_message(platform, user_id, "user", user_message)
            save_message(platform, user_id, "assistant", reply)
            save_log("email_collected", platform, user_id, user_email)

            return jsonify({"reply": reply})

        # =========================================
        # NORMAL AI PROCESSING
        # =========================================

        # Get AI reply + detect meeting/cancellation
        reply, meeting_data, cancel_data = process_with_ai(
            platform, user_id, user_message, business_context
        )

        # Save assistant reply to DB
        save_message(platform, user_id, "assistant", reply)
        save_log("reply_sent", platform, user_id, reply)

        # =========================================
        # HANDLE CANCELLATION
        # =========================================

        if cancel_data:
            date_str = cancel_data.get("date", "")
            event_id = cancel_meeting_in_db(platform, user_id, date_str)

            if event_id is not None:
                cancel_google_event(event_id)

                # Send cancellation email if we have user's email
                user_email = get_user_email(platform, user_id)
                if user_email:
                    send_email_notification(
                        to_email=user_email,
                        subject="Meeting Cancelled",
                        body=(
                            f"Hello,\n\n"
                            f"Your meeting scheduled on {date_str} has been cancelled.\n\n"
                            f"If you'd like to reschedule, please reach out.\n\n"
                            f"Best regards,\n"
                            f"Executive Assistant"
                        )
                    )
                    reply = f"Your meeting on {date_str} has been successfully cancelled. A confirmation has been sent to {user_email}."
                else:
                    reply = f"Your meeting on {date_str} has been successfully cancelled."

                save_log("meeting_cancelled", platform, user_id,
                         f"Cancelled meeting on {date_str}")
            else:
                reply = f"I couldn't find a scheduled meeting on {date_str}. Please check the date and try again."
                save_log("meeting_cancel_notfound", platform, user_id,
                         f"No meeting found for date: {date_str}")

            save_message(platform, user_id, "assistant", reply)

        # =========================================
        # HANDLE NEW MEETING SCHEDULING
        # =========================================

        elif meeting_data:
            title = meeting_data.get("title", "Meeting")
            date_str = meeting_data.get("date", "")
            time_str = meeting_data.get("time", "")
            description = meeting_data.get("description", "")

            # Check if we already have user's email
            user_email = get_user_email(platform, user_id)

            if user_email:
                # Already have email — check availability and schedule directly
                is_free = check_calendar_availability(date_str, time_str)

                if not is_free:
                    reply = f"I'm sorry, the CEO is not available on {date_str} at {time_str}. Could you suggest another date and time?"
                    save_message(platform, user_id, "assistant", reply)
                else:
                    event_id, meet_link = create_calendar_event(
                        title, date_str, time_str, description,
                        attendee_email=user_email
                    )

                    save_meeting(
                        platform, user_id, title, date_str, time_str,
                        description, event_id or "", meet_link or ""
                    )

                    # Send confirmation email
                    send_email_notification(
                        to_email=user_email,
                        subject=f"Meeting Confirmed: {title}",
                        body=(
                            f"Hello,\n\n"
                            f"Your meeting has been confirmed.\n\n"
                            f"Title: {title}\n"
                            f"Date: {date_str}\n"
                            f"Time: {time_str}\n"
                            f"Google Meet: {meet_link or 'Link will be shared shortly'}\n\n"
                            f"Best regards,\n"
                            f"Executive Assistant"
                        )
                    )

                    save_log("meeting_scheduled", platform, user_id,
                             f"{title} on {date_str} at {time_str}")

                    if meet_link:
                        reply += f"\n\nGoogle Meet link: {meet_link}"
                    reply += f"\n\nA calendar invite has been sent to {user_email}."
                    save_message(platform, user_id, "assistant", reply)

            else:
                # No email yet — save as pending and ask for email
                save_pending_meeting(platform, user_id, title, date_str, time_str, description)
                reply = f"I'd be happy to schedule that meeting. Could you please share your email address so I can send you the calendar invite and Google Meet link?"
                save_message(platform, user_id, "assistant", reply)
                save_log("pending_meeting_created", platform, user_id,
                         f"{title} on {date_str} at {time_str}")

        print("\n✅ FINAL REPLY:", reply)

        return jsonify({"reply": reply})

    except Exception as e:

        print("\n❌ GOVERNOR PROCESSING ERROR:", str(e))
        save_log("error", platform, user_id, str(e))

        return jsonify({"reply": "I'm temporarily unavailable. Please try again shortly."})


# =========================================
# WEBSITE SCRAPER — for future CEO onboarding
# When a CEO adds their website, we scrape it
# and store it as business_context in the DB
# =========================================

@app.route("/onboard/scrape_website", methods=["POST"])
def scrape_website():
    try:
        data = request.get_json()
        url = data.get("url", "")
        ceo_id = data.get("ceo_id", "")

        if not url or not ceo_id:
            return jsonify({"error": "url and ceo_id are required"}), 400

        import urllib.request
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self.skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ["script", "style", "nav", "footer"]:
                    self.skip = True
            def handle_endtag(self, tag):
                if tag in ["script", "style", "nav", "footer"]:
                    self.skip = False
            def handle_data(self, data):
                if not self.skip and data.strip():
                    self.text.append(data.strip())

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")

        parser = TextExtractor()
        parser.feed(html)
        raw_text = " ".join(parser.text)[:3000]  # limit to 3000 chars

        # Store in DB
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS business_profiles (
                ceo_id VARCHAR(255) PRIMARY KEY,
                website_url VARCHAR(500),
                business_context TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            INSERT INTO business_profiles (ceo_id, website_url, business_context)
            VALUES (%s, %s, %s)
            ON CONFLICT (ceo_id) DO UPDATE
            SET website_url=%s, business_context=%s
        """, (ceo_id, url, raw_text, url, raw_text))
        conn.commit()
        cur.close()
        conn.close()

        save_log("website_scraped", "system", ceo_id, url)

        return jsonify({
            "success": True,
            "ceo_id": ceo_id,
            "context_length": len(raw_text),
            "preview": raw_text[:200]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/clear_pending/<platform>/<user_id>", methods=["GET"])
def clear_pending(platform, user_id):
    try:
        delete_pending_meeting(platform, user_id)
        return jsonify({"success": True, "message": f"Cleared pending meeting for {user_id}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================================
# DASHBOARD API ENDPOINTS
# =========================================

@app.route("/dashboard/conversations", methods=["GET"])
def get_conversations():
    try:
        platform = request.args.get("platform")
        user_id = request.args.get("user_id")
        limit = int(request.args.get("limit", 50))

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        query = "SELECT * FROM conversations WHERE 1=1"
        params = []

        if platform:
            query += " AND platform=%s"
            params.append(platform)

        if user_id:
            query += " AND user_id=%s"
            params.append(user_id)

        query += " ORDER BY timestamp DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        return jsonify({
            "conversations": [dict(r) for r in rows],
            "count": len(rows)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard/meetings", methods=["GET"])
def get_meetings():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM meetings ORDER BY created_at DESC")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        return jsonify({
            "meetings": [dict(r) for r in rows],
            "count": len(rows)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard/logs", methods=["GET"])
def get_logs():
    try:
        limit = int(request.args.get("limit", 100))

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM logs ORDER BY timestamp DESC LIMIT %s",
            (limit,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        return jsonify({
            "logs": [dict(r) for r in rows],
            "count": len(rows)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard/stats", methods=["GET"])
def get_stats():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT COUNT(*) as total FROM conversations WHERE role='user'")
        total_messages = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) as total FROM meetings")
        total_meetings = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(DISTINCT user_id) as total FROM conversations")
        total_users = cur.fetchone()["total"]

        cur.execute(
            "SELECT COUNT(*) as total FROM meetings WHERE status='scheduled'"
        )
        upcoming_meetings = cur.fetchone()["total"]

        cur.close()
        conn.close()

        return jsonify({
            "total_messages": total_messages,
            "total_meetings": total_meetings,
            "total_users": total_users,
            "upcoming_meetings": upcoming_meetings
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================================
# KEEP ALIVE PINGER
# =========================================

def keep_alive():
    while True:
        time.sleep(600)  # ping every 10 minutes
        urls = [
            "https://governor-ai-1odr.onrender.com",
            "https://fb-webhook-bot-uhxb.onrender.com",
            "https://gmail-bot-k5cc.onrender.com",
            os.getenv("GMAIL_BOT_URL", "")
        ]
        for url in urls:
            if not url:
                continue
            try:
                requests.get(url, timeout=10)
                print(f"✅ Keep-alive ping: {url}")
            except Exception as e:
                print(f"⚠️ Keep-alive failed for {url}: {str(e)}")


# =========================================
# START SERVER
# =========================================

# Initialize DB on startup
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print("❌ DB init error:", str(e))

# Start keep-alive thread
ping_thread = threading.Thread(target=keep_alive, daemon=True)
ping_thread.start()
print("✅ Keep-alive pinger started")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
