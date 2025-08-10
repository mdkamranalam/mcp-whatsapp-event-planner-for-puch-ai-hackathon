import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from dateutil.parser import parse as dateparse
from aiohttp import web
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from pydantic import Field
from typing import Annotated, List
from twilio.rest import Client as TwilioRestClient
from dotenv import load_dotenv
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load env vars
load_dotenv()

TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")

assert TOKEN, "Please set AUTH_TOKEN in .env"
assert MY_NUMBER, "Please set MY_NUMBER in .env"

# Twilio credentials (optional)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

# Twilio client init
_twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    _twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# MCP Auth Provider
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(token=token, client_id="puch-client", scopes=["*"], expires_at=None)
        return None

mcp = FastMCP("WhatsApp Event Planner", auth=SimpleBearerAuthProvider(TOKEN))

reminders_sent = set()  # store tuples like (event_id, phone) to avoid duplicate reminders

# File persistence
EVENTS_FILE = "events.json"
events = {}  # event_id -> event dict

def load_events():
    global events
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            try:
                events = json.load(f)
            except Exception as e:
                logger.error(f"Error loading events file: {e}")
                events = {}
    else:
        events = {}

def save_events():
    try:
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving events file: {e}")

# Normalize phone to whatsapp:+ format
def normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("whatsapp:"):
        return phone
    if phone.startswith("+"):
        return "whatsapp:" + phone
    return phone

# Send WhatsApp message (simulate if no Twilio)
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
    else:
        # Simulate send
        logger.info(f"[SIMULATED SEND] To: {to_norm}\n{body}\n---")
        return {"status": "simulated"}

async def reminder_background_task():
    while True:
        now = datetime.now()
        check_window_start = now + timedelta(minutes=55)  # start 55 min from now
        check_window_end = now + timedelta(minutes=65)    # end 65 min from now (1 hour ±5 min)

        for event_id, ev in events.items():
            try:
                event_dt = dateparse(ev["datetime"])
                if check_window_start <= event_dt <= check_window_end:
                    # Send reminders to RSVP=YES guests
                    rsvps = ev.get("rsvps", {})
                    for phone, rsvp_info in rsvps.items():
                        if rsvp_info.get("response", "").upper() == "YES":
                            normalized_phone = normalize_phone(phone)
                            if (event_id, normalized_phone) not in reminders_sent:
                                # Send WhatsApp reminder
                                msg = (
                                    f"⏰ Reminder: Event *{ev['title']}* starts at "
                                    f"{event_dt.strftime('%Y-%m-%d %H:%M')}. See you there!"
                                )
                                try:
                                    send_whatsapp_message(normalized_phone, msg)
                                    reminders_sent.add((event_id, normalized_phone))
                                    logger.info(f"Sent reminder for event {event_id} to {normalized_phone}")
                                except Exception as e:
                                    logger.error(f"Failed to send reminder to {normalized_phone}: {e}")
            except Exception as e:
                logger.error(f"Error processing reminders for event {event_id}: {e}")

        await asyncio.sleep(300)  # wait 5 minutes before checking again

# Tools

@mcp.tool
async def validate() -> str:
    # Return digits-only number string from MY_NUMBER for auth
    digits = "".join(ch for ch in MY_NUMBER if ch.isdigit())
    return digits

@mcp.tool
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
    events[event_id] = event
    save_events()

    # Send invites
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

@mcp.tool
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

@mcp.tool
async def record_rsvp(event_id: Annotated[str, Field(description="Event ID")],
                      phone: Annotated[str, Field(description="Phone whatsapp:+...")],
                      response: Annotated[str, Field(description="YES / NO / MAYBE")]) -> str:
    response_up = response.strip().upper()
    if response_up not in {"YES", "NO", "MAYBE"}:
        raise McpError(ErrorData(code=400, message="Invalid RSVP response"))
    ev = events.get(event_id)
    if not ev:
        raise McpError(ErrorData(code=404, message="Event not found"))
    normalized_phone = normalize_phone(phone)
    ev["rsvps"][normalized_phone] = {"response": response_up, "time": datetime.now().isoformat()}
    save_events()

    # Notify creator
    creator = ev.get("creator")
    try:
        send_whatsapp_message(creator, f"✅ RSVP from {normalized_phone}: {response_up} for event {event_id}")
    except Exception:
        pass
    return f"RSVP recorded for event {event_id}: {normalized_phone} → {response_up}"

@mcp.tool
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

@mcp.tool
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


# Twilio webhook handler
async def handle_twilio_webhook(request):
    data = await request.post()
    from_num = data.get("From", "")
    body = data.get("Body", "").strip()

    from_num = normalize_phone(from_num)
    reply = "Sorry, I didn't understand that."

    if body.startswith("/create_event"):
        # Format:
        # /create_event Title;YYYY-MM-DD HH:MM;Location;Description;whatsapp:+111,whatsapp:+222
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
        # Format: /rsvp event_id YES
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
            "/rsvp event_id YES|NO|MAYBE"
        )

    # Respond with TwiML
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{reply}</Message></Response>'
    return web.Response(text=twiml, content_type="application/xml")

# Build aiohttp app with webhook route
def create_app():
    app = web.Application()
    app.router.add_post("/twilio-webhook", handle_twilio_webhook)
    return app

async def main():
    logger.info("Loading events from file...")
    load_events()
    asyncio.create_task(reminder_background_task())
    logger.info("Starting MCP server and webhook...")
    aio_app = create_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Webhook listening on http://0.0.0.0:8080/twilio-webhook")

    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting...")
