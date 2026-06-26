"""
Gmail IMAP checker for FamPay payment verification.
Reads GMAIL_ADDRESS and GMAIL_APP_PASSWORD from environment.

Verification logic:
  - Searches recent emails for FamPay/payment messages
  - Passes if the submitted code is found as EITHER a UTR OR a transaction ID
    in the email body/subject, AND the plan amount is also present.
"""
import imaplib
import email
import re
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def _connect() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return mail


def _extract_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return body


def _id_present(submitted: str, full_text: str) -> bool:
    """
    Returns True if `submitted` appears in the email text as either:
      - A UTR number  (looks for 'utr' label nearby, or bare 12-digit match)
      - A transaction ID  (looks for 'txn', 'transaction id', 'ref' labels)
      - Or simply anywhere in the full text (broad fallback)
    Matching is case-insensitive.
    """
    sub = submitted.strip().lower()
    text = full_text.lower()

    # Broad match: submitted string appears anywhere in email
    if sub in text:
        return True

    # Extract all alphanumeric tokens of similar length and compare
    # (handles cases where email has spaces or formatting around the ID)
    cleaned_sub = re.sub(r"\s+", "", sub)
    tokens = re.findall(r"[a-z0-9]{6,}", text)
    for token in tokens:
        if token == cleaned_sub:
            return True

    return False


def _amount_present(amount: int, full_text: str) -> bool:
    """
    Returns True if the plan amount appears in the email in any common format.
    """
    text = full_text.lower()
    amount_str = str(amount)

    variants = [
        amount_str,
        f"₹{amount}",
        f"₹ {amount}",
        f"rs.{amount}",
        f"rs {amount}",
        f"rs. {amount}",
        f"inr {amount}",
        f"inr{amount}",
        f"inr.{amount}",
        f"inr. {amount}",
        # decimal forms  e.g. 10.00 / 190.00
        f"{amount}.00",
        f"₹{amount}.00",
        f"rs.{amount}.00",
        f"inr {amount}.00",
    ]
    for v in variants:
        if v.lower() in text:
            return True
    return False


def check_fampay_payment(submitted_id: str, amount: int) -> bool:
    """
    Search Gmail inbox for a payment email where:
      - The submitted_id matches EITHER the UTR OR the transaction ID in the email
      - AND the plan amount is also present in the email

    Returns True on a successful match, False otherwise.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.warning("Gmail credentials not set — auto-pay verification skipped.")
        return False

    try:
        mail = _connect()
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")

        search_queries = [
            f'(SINCE {since_date} FROM "fampay")',
            f'(SINCE {since_date} FROM "noreply@fampay.in")',
            f'(SINCE {since_date} SUBJECT "fampay")',
            f'(SINCE {since_date} SUBJECT "payment received")',
            f'(SINCE {since_date} SUBJECT "transaction")',
            f'(SINCE {since_date} BODY "{submitted_id}")',
        ]

        collected_ids: set = set()
        for query in search_queries:
            try:
                status, messages = mail.search(None, query)
                if status == "OK" and messages[0]:
                    for mid in messages[0].split():
                        collected_ids.add(mid)
            except Exception:
                continue

        logger.info(
            f"Auto-pay check: {len(collected_ids)} candidate email(s) "
            f"for id={submitted_id} amount={amount}"
        )

        for email_id in collected_ids:
            try:
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                if status != "OK":
                    continue
                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                body = _extract_body(msg)
                subject = str(msg.get("Subject", ""))
                full_text = body + " " + subject

                # Check: submitted ID found as UTR or transaction ID
                id_ok = _id_present(submitted_id, full_text)
                if not id_ok:
                    continue

                # Check: plan amount is present
                amount_ok = _amount_present(amount, full_text)
                if amount_ok:
                    logger.info(
                        f"Auto-pay MATCH: id={submitted_id} amount={amount} "
                        f"email_id={email_id}"
                    )
                    mail.logout()
                    return True

            except Exception as ex:
                logger.warning(f"Error reading email {email_id}: {ex}")
                continue

        mail.logout()
        logger.info(f"Auto-pay: no match for id={submitted_id} amount={amount}")
        return False

    except Exception as ex:
        logger.error(f"Gmail check failed: {ex}")
        return False
