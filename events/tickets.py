"""
Individual event tickets — one identifiable ticket per ticket bought.

An EventOrder is one purchase ("2 × Adult, 1 × Child"). Its tickets are
derived from it, never stored: ticket N of an order is

    ticket ID          <order ID>-NN          e.g. K7M2QX-01, K7M2QX-02
    verification code  8 characters, an HMAC of the ticket ID keyed on the
                       server's SECRET_KEY — it cannot be made up without
                       the server, so a hand-edited PDF does not verify
    QR payload         BMVT1:<ticket ID>:<code>

The order ID is already unique across every booking type (BookingCode), so
the ticket IDs are unique too, and the same order always yields the same
tickets — the PDF can be rebuilt any time (booking history, a resend) and
shows identical IDs and QR codes. Numbering follows the order's line items in
order: "2 × Adult, 1 × Child" -> 01 Adult, 02 Adult, 03 Child.

Tickets exist only for a VERIFIED PAID order: status 'confirmed' and a
Razorpay payment id. A pending, failed, cancelled, refunded or removed order
has no valid tickets (issuable() is False), and verification of a ticket
always re-reads the order, so a ticket from an order refunded after its PDF
was sent stops admitting anyone.

Entry stays per ORDER (bookings/checkin.py): scanning any ticket of an order
shows that order, and checking it in admits the whole party — the rule the
gate already had. The ticket number tells the door which ticket was shown.
"""
import base64
import hashlib
import hmac
import re

from django.conf import settings

QR_PREFIX = 'BMVT1'
CODE_LENGTH = 8
# K7M2QX-01, eo_3f8a92c1d04e-12, BK-… never (event orders only).
_TICKET_ID = re.compile(r'([A-Za-z0-9_]{6,20})-(\d{2,3})')
_QR = re.compile(r'BMVT1:([A-Za-z0-9_]{6,20}-\d{2,3}):([A-Z2-7]{%d})' % CODE_LENGTH)


def issuable(order):
    """Only a confirmed, gateway-verified order has valid tickets."""
    return order.status == 'confirmed' and bool(order.razorpay_payment_id)


def ticket_id(order_id, number, total):
    width = max(2, len(str(total)))
    return f'{order_id}-{number:0{width}d}'


def verification_code(tid):
    key = (settings.SECRET_KEY + ':event-ticket:v1').encode()
    digest = hmac.new(key, str(tid).encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode()[:CODE_LENGTH]


def qr_payload(tid):
    return f'{QR_PREFIX}:{tid}:{verification_code(tid)}'


def tickets_for(order):
    """[{number, total, ticketId, code, qr, typeKey, typeLabel, price}] — one
    per ticket (Σ qty of the line items). [] when the order is not issuable."""
    if not issuable(order):
        return []
    lines = []
    for item in order.items or []:
        if not isinstance(item, dict):
            continue
        qty = int(item.get('qty') or 0)
        for _ in range(max(qty, 0)):
            lines.append(item)
    total = len(lines)
    rows = []
    for number, item in enumerate(lines, start=1):
        tid = ticket_id(order.id, number, total)
        rows.append({
            'number': number,
            'total': total,
            'ticketId': tid,
            'code': verification_code(tid),
            'qr': qr_payload(tid),
            'typeKey': str(item.get('key') or ''),
            'typeLabel': str(item.get('label') or 'Ticket'),
            'price': int(item.get('price') or 0),
            'seats': int(item.get('seatsPerTicket') or 1),
        })
    return rows


class TicketRef:
    """A parsed ticket reference: the order it belongs to, which ticket, and
    whether the verification code (QR) was present and genuine."""

    def __init__(self, order_id, number, code=None):
        self.order_id = order_id
        self.number = number
        self.code = code
        self.tid = None

    @property
    def signed(self):
        return self.code is not None


def parse(raw):
    """TicketRef for a scanned QR payload or a typed ticket ID, else None.

    Forgiving about spaces and case in a typed ID ("k7m2qx - 01"); the QR
    payload is matched exactly (scanners type it verbatim)."""
    text = ''.join(str(raw or '').split())
    match = _QR.fullmatch(text)
    if match:
        tid, code = match.groups()
        order_id, number = tid.rsplit('-', 1)
        ref = TicketRef(order_id, int(number), code)
        ref.tid = tid
        return ref
    if text.upper().startswith(QR_PREFIX + ':'):
        # Shaped like our QR but malformed: never fall back to an ID match.
        return TicketRef(None, 0, code='')
    match = _TICKET_ID.fullmatch(text)
    if match:
        order_id, number = match.groups()
        # Order IDs are upper-case 6-character codes, or legacy 'eo_<hex>'.
        order_id = order_id.lower() if order_id.lower().startswith('eo_') else order_id.upper()
        ref = TicketRef(order_id, int(number))
        ref.tid = f'{order_id}-{number}'
        return ref
    return None


def resolve(ref):
    """(order, ticket row) for a parsed ref, or (None, reason) where reason is
    'forged' (bad / malformed code) or 'unknown' (no such order or ticket).
    The ticket row is None for a real order whose tickets are not valid now
    (unpaid, refunded …) — the caller's status checks then explain why."""
    from .models import EventOrder

    if ref.order_id is None:
        return None, 'forged'
    if ref.signed and not hmac.compare_digest(ref.code, verification_code(ref.tid)):
        return None, 'forged'
    order = EventOrder.objects.filter(pk=ref.order_id).first()
    if order is None:
        return None, 'unknown'
    count = sum(
        int(item.get('qty') or 0) for item in (order.items or []) if isinstance(item, dict)
    )
    if not 1 <= ref.number <= count or ref.tid != ticket_id(order.id, ref.number, count):
        return None, 'unknown'
    rows = tickets_for(order)
    ticket = rows[ref.number - 1] if rows else None
    return order, ticket


def ticket_summary(ticket):
    """What the gate / admin sees about the ticket that was presented."""
    if not ticket:
        return None
    return {
        'ticketId': ticket['ticketId'],
        'number': ticket['number'],
        'of': ticket['total'],
        'type': ticket['typeLabel'],
    }
