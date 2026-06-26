import os

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_BOT_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
ADMIN_USERNAME = "shubhxseller"
ADMIN_TG_ID = int(os.environ.get("ADMIN_TG_ID", "0"))
UPI_ID = os.environ.get("UPI_ID", "your-upi@upi")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

# Gmail credentials for FamPay auto-pay verification
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

BASE_DIR = os.path.dirname(__file__)
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
DATA_DIR = os.path.join(BASE_DIR, "data")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

WELCOME_IMAGE = os.path.join(ASSETS_DIR, "welcome.jpg")
QR_IMAGE = os.path.join(ASSETS_DIR, "payment_qr.jpg")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

FREE_DM_LIMIT = 100
FREE_ACCEPT_LIMIT = 20   # free users can accept up to this many join requests per action

PLANS = {
    "1d":  {"label": "1 Day",    "days": 1,   "price": 10},
    "3d":  {"label": "3 Days",   "days": 3,   "price": 30},
    "7d":  {"label": "7 Days",   "days": 7,   "price": 60},
    "15d": {"label": "15 Days",  "days": 15,  "price": 100},
    "1m":  {"label": "1 Month",  "days": 30,  "price": 190},
}

TUTORIAL_TEXT = (
    "📖 *Tutorial & Terms*\n\n"
    "*How to Login:*\n"
    "Step 1 — Enter your phone number with country code (e.g. +91XXXXXXXXXX)\n"
    "Step 2 — Enter the OTP sent to your Telegram\n"
    "Step 3 — Enter your 2FA password (if set)\n"
    "Your account will be added after these steps.\n\n"
    "*How to Use:*\n"
    "Step 1 — Go to *Set Message* and enter the message/link/image you want to send\n"
    "Step 2 — Tap *Start Mass Campaign* — the bot will send your message to all DMs and chats\n\n"
    "*Free Plan Limits:*\n"
    "After adding your account, you can send up to *100 DMs and 100 group chats* for free.\n"
    "After that, you must purchase a premium plan to continue.\n\n"
    "*Premium Plans:*\n"
    "Once the admin approves your payment, you get *unlimited sends* for the plan duration.\n\n"
    "*Terms:*\n"
    "• Do not use this bot for spam or illegal activity.\n"
    "• The team is not responsible for misuse.\n"
    "• Premium plans are non-refundable.\n"
    "• By using this bot, you agree to these terms.\n\n"
    "👤 Support: @shubhxseller"
)
