import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from dateutil.parser import parse as dateparse
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from pydantic import Field
from typing import Annotated, List, Dict, Any
from twilio.rest import Client as TwilioRestClient
from twilio.twiml.messaging_response import MessagingResponse
from fastapi import FastAPI, Request
from dotenv import load_dotenv
import logging
# import sqlite3  # Uncomment for SQLite option

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('mcp_server.log')]
)
logger = logging.getLogger(__name__)

# Load env vars
load_dotenv()

TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")

assert TOKEN, "Please set AUTH_TOKEN in .env"
assert MY_NUMBER, "Please set MY_NUMBER in .env"

# Twilio client init
_twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER:
    _twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
else:
    logger.warning("Twilio credentials missing; WhatsApp messages will be simulated")

# MCP Auth Provider
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        logger.debug(f"Validating token: {token[:10]}...")
        if token == self.token:
            logger.info("Token validated successfully")
            return AccessToken(token=token, client_id="puch-client", scopes=["*"], expires_at=None)
        logger.warning(f"Invalid token: {token[:10]}...")
        return None

# MCP Server Setup
mcp = FastMCP("WhatsApp Event Planner", auth=SimpleBearerAuthProvider(TOKEN), stateless_http=True)

# FastAPI App
app = mcp.sse_app()  # Use sse_app() for SSE transport; replace with mcp.app() for streamable-http

# Event Storage (In-memory; uncomment SQLite for persistent storage)
events: Dict[str, Any] = {}
reminders_sent = set()  # (event_id, phone) tuples

"""
# SQLite Option
def init_db():
    conn = sqlite3.connect("events.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS events
                 (id TEXT PRIMARY KEY, data TEXT)''')
    conn.commit()
    conn.close()

def load_events():
    global events
    conn = sqlite3.connect("events.db")
    c = conn.cursor()
    c.execute("SELECT id, data FROM events")
    events = {row[0]: json.loads(row[1]) for row in c.fetchall()}
    conn.close()

def save_event(event_id: str, event: Dict[str, Any]):
    conn = sqlite3.connect("events.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO events (id, data) VALUES (?, ?)",
              (event_id, json.dumps(event, default=str)))
    conn.commit()
    conn.close()
"""

def load_events():
    global events
    # For in-memory, reset events (Render ephemeral filesystem)
    events = {}
    # Uncomment for SQLite
    # init_db()
    # load_events()

def save_event(event_id: str, event: Dict[str, Any]):
    events[event_id] = event
    # Uncomment for SQLite
    # save_event(event_id, event)

# Normalize phone to whatsapp:+ format
def normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("whatsapp:"):
        return phone
    if phone.startswith("+"):
        return "whatsapp:" + phone
    return phone

# Send WhatsApp message
def send_whatsapp_message(to: str, body: str) -> dict:
    to_norm = normalize_phone(to)
    if _twilio_client and TWILIO_WHATSAPP_NUMBER:
        try:
            msg = _twilio_client.messages.create(
                body=body,
                from_=TWILIO_WHATSAPP_NUMBER,
                to=to_norm,
            )
            logger.info(f"Sent WhatsApp message to {to_norm}, SID: {msg.sid}")
            return {"status": "sent", "sid": msg.sid}
        except Exception as e:
            logger.error(f"Twilio send error: {e}")
            return {"status": "error", "error": str(e)}
    logger.info(f"[SIMULATED SEND] To: {to_norm}\n{body}\n---")
    return {"status": "simulated"}

# Reminder Task
async def reminder_background_task():
    while True:
        now = datetime.now()
        check_window_start = now + timedelta(minutes=55)
        check_window_end = now + timedelta(minutes=65)

        for event_id, ev in events.items():
            try:
                event_dt = dateparse(ev["datetime"])
                if check_window_start <= event_dt <= check_window_end:
                    rsvps = ev.get("rsvps", {})
                    for phone, rsvp_info in rsvps.items():
                        if rsvp_info.get("response", "").upper() == "YES":
                            normalized_phone = normalize_phone(phone)
                            if (event_id, normalized_phone) not in reminders_sent:
                                msg = (
                                    f"⏰ Reminder: Event *{ev['title']}* starts at "
                                    f"{event_dt.strftime('%Y-%m-%d %H:%M')}. See you there!"
                                )
                                send_whatsapp_message(normalized_phone, msg)
                                reminders_sent.add((event_id, normalized_phone))
                                logger.info(f"Sent reminder for event {event_id} to {normalized_phone}")
            except Exception as e:
                logger.error(f"Error processing reminders for event {event_id}: {e}")

        await asyncio.sleep(300)  # Check every 5 minutes

# MCP Tools
@mcp.tool(description="Validate bearer token and return phone number")
async def validate() -> str:
    digits = "".join(ch for ch in MY_NUMBER if ch.isdigit())
    return digits

@mcp.tool(description="Create an event and send invites")
async def create_event(
    creator: Annotated[str, Field(description="Creator phone number")],
    title: Annotated[str, Field(description="Event title")],
    datetime_str: Annotated[str, Field(description="Event datetime ISO or 'YYYY-MM-DD HH:MM'")],
    location: Annotated[str, Field(description="Event location")] = "",
    description: Annotated[str | None, Field(description="Optional description")] = None,
    attendees: Annotated[List[str] | None, Field(description="List of whatsapp:+phone")] = None,
) -> str:
    try:
        dt = dateparse(datetime_str)
    except Exception:
        raise McpError(ErrorData(code=400, message="Invalid datetime format"))

    event_id = str(uuid.uuid4())
    event = {
        "id": event_id,
        "title": title,
        "datetime": dt.isoformat(),
        "location": location,
        "description": description or "",
        "creator": normalize_phone(creator),
        "attendees": [normalize_phone(a) for a in (attendees or [])],
        "rsvps": {}
    }
    save_event(event_id, event)

    for att in event["attendees"]:
        msg = (
            f"📅 Event: {title}\n"
            f"🗓 When: {dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"📍 Where: {location}\n"
            f"📝 {description or ''}\n\n"
            "Reply with:\n"
            f"/rsvp {event_id} YES\n"
            f"/rsvp {event_id} NO\n"
            f"/rsvp {event_id} MAYBE"
        )
        send_whatsapp_message(att, msg)

    return f"Event {event_id} created and invites sent to {len(event['attendees'])} attendees."

@mcp.tool(description="List all events")
async def list_events() -> str:
    if not events:
        return "No events found."
    lines = []
    now = datetime.now()
    for e in events.values():
        dt = dateparse(e["datetime"])
        status = "Upcoming" if dt > now else "Past"
        lines.append(f"{e['id']}: {e['title']} at {dt.strftime('%Y-%m-%d %H:%M')} ({status})")
    return "\n".join(lines)

@mcp.tool(description="Record RSVP for an event")
async def record_rsvp(
    event_id: Annotated[str, Field(description="Event ID")],
    phone: Annotated[str, Field(description="Phone whatsapp:+...")],
    response: Annotated[str, Field(description="YES / NO / MAYBE")]
) -> str:
    response_up = response.strip().upper()
    if response_up not in {"YES", "NO", "MAYBE"}:
        raise McpError(ErrorData(code=400, message="Invalid RSVP response"))
    ev = events.get(event_id)
    if not ev:
        raise McpError(ErrorData(code=404, message="Event not found"))
    normalized_phone = normalize_phone(phone)
    ev["rsvps"][normalized_phone] = {"response": response_up, "time": datetime.now().isoformat()}
    save_event(event_id, ev)

    creator = ev.get("creator")
    try:
        send_whatsapp_message(creator, f"✅ RSVP from {normalized_phone}: {response_up} for event {event_id}")
    except Exception:
        pass
    return f"RSVP recorded for event {event_id}: {normalized_phone} → {response_up}"

@mcp.tool(description="Get event details")
async def event_details(event_id: Annotated[str, Field(description="Event ID")]) -> str:
    ev = events.get(event_id)
    if not ev:
        return f"Event {event_id} not found."

    dt = dateparse(ev["datetime"])
    rsvps = ev.get("rsvps", {})
    rsvp_counts = {"YES": 0, "NO": 0, "MAYBE": 0}
    for r in rsvps.values():
        resp = r.get("response", "").upper()
        if resp in rsvp_counts:
            rsvp_counts[resp] += 1

    details = (
        f"Event {event_id}: {ev['title']}\n"
        f"When: {dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"Where: {ev['location']}\n"
        f"Description: {ev['description']}\n"
        f"Created by: {ev['creator']}\n\n"
        f"RSVP Summary:\n"
        f"YES: {rsvp_counts['YES']}\n"
        f"NO: {rsvp_counts['NO']}\n"
        f"MAYBE: {rsvp_counts['MAYBE']}\n"
    )
    return details

@mcp.tool(description="List RSVPs for an event")
async def rsvp_list(event_id: Annotated[str, Field(description="Event ID")]) -> str:
    ev = events.get(event_id)
    if not ev:
        return f"Event {event_id} not found."

    rsvps = ev.get("rsvps", {})
    if not rsvps:
        return "No RSVPs yet."

    lines = ["RSVP List:"]
    for phone, rsvp_info in rsvps.items():
        resp = rsvp_info.get("response", "")
        time = rsvp_info.get("time", "")
        lines.append(f"{phone}: {resp} (at {time})")
    return "\n".join(lines)

# Twilio Webhook
@app.post("/twilio-webhook")
async def handle_twilio_webhook(request: Request):
    logger.debug("Received Twilio webhook request")
    form = await request.form()
    from_num = form.get("From", "")
    body = form.get("Body", "").strip()
    from_num = normalize_phone(from_num)
    reply = "Sorry, I didn't understand that."

    if body.startswith("/create_event"):
        try:
            cmd, args = body.split(" ", 1)
            parts = args.split(";")
            title = parts[0]
            dt_str = parts[1]
            loc = parts[2] if len(parts) > 2 else ""
            desc = parts[3] if len(parts) > 3 else ""
            atts = parts[4].split(",") if len(parts) > 4 else []
            res = await create_event(from_num, title, dt_str, loc, desc, atts)
            reply = f"✅ {res}"
        except Exception as e:
            logger.error(f"Error creating event: {e}")
            reply = ("Error creating event. Use:\n"
                     "/create_event Title;YYYY-MM-DD HH:MM;Location;Description;whatsapp:+111,whatsapp:+222")

    elif body.startswith("/list_events"):
        reply = await list_events()

    elif body.startswith("/rsvp"):
        try:
            _, ev_id, resp = body.split()
            reply = await record_rsvp(ev_id, from_num, resp)
        except Exception:
            reply = "Use /rsvp event_id YES|NO|MAYBE"

    elif body.startswith("/event_details"):
        parts = body.split()
        if len(parts) != 2:
            reply = "Usage: /event_details <event_id>"
        else:
            reply = await event_details(parts[1])

    elif body.startswith("/rsvp_list"):
        parts = body.split()
        if len(parts) != 2:
            reply = "Usage: /rsvp_list <event_id>"
        else:
            reply = await rsvp_list(parts[1])

    else:
        reply = (
            "Commands:\n"
            "/create_event Title;YYYY-MM-DD HH:MM;Location;Description;whatsapp:+111,whatsapp:+222\n"
            "/list_events\n"
            "/rsvp event_id YES|NO|MAYBE\n"
            "/event_details event_id\n"
            "/rsvp_list event_id"
        )

    response = MessagingResponse()
    response.message(reply)
    return response

# Main
async def main():
    logger.info("Loading events...")
    load_events()
    logger.info("Starting reminder task...")
    asyncio.create_task(reminder_background_task())
    logger.info("Starting MCP server...")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)