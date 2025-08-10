from twilio.rest import Client
import os
from dotenv import load_dotenv

load_dotenv()  # Load from .env

account_sid = os.environ["TWILIO_ACCOUNT_SID"]
auth_token = os.environ["TWILIO_AUTH_TOKEN"]
from_whatsapp_number = os.environ["TWILIO_WHATSAPP_FROM"]
to_whatsapp_number = os.environ["TWILIO_WHATSAPP_TO"]

client = Client(account_sid, auth_token)

message = client.messages.create(
    body="Hello from Twilio WhatsApp!",
    from_=from_whatsapp_number,
    to=to_whatsapp_number
)

print("✅ Message sent! SID:", message.sid)
