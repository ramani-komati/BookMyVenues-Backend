"""
The event ticket PDF — one A4 page per ticket (events/tickets.py), each with
its own ticket ID, verification code and QR code.

    render(order) -> bytes      raises TicketUnavailable for an order that
                                has no valid tickets (not verified-paid)

Drawn with ReportLab (requirements.txt), including the QR code, so nothing
here calls out to another service. The only network access is the OPTIONAL
event poster: fetched over https from the platform's own image storage, with
a short timeout and a size cap, and simply left out on any failure — a slow
image host can never stop a paid customer's ticket from being made.
"""
import datetime
import io
import logging
from urllib.parse import urlparse

from django.conf import settings

from bookings.slots import IST

from . import tickets as tickets_mod

logger = logging.getLogger(__name__)

NAVY = '#06152C'
MUTED = '#5B6475'
LINE = '#E3E7EE'

POSTER_TIMEOUT = 4          # seconds
POSTER_MAX_BYTES = 3 * 1024 * 1024
BRAND = 'HAPVOO'


class TicketUnavailable(Exception):
    """The order has no valid tickets (unpaid, failed, cancelled, refunded)."""


def _rupees(value):
    return f'Rs. {int(value or 0):,}'   # the built-in fonts have no ₹ glyph


def _when(order):
    day = f'{order.date:%A, %d %B %Y}'
    if order.start_time and order.end_time:
        return day, f'{order.start_time:%I:%M %p} - {order.end_time:%I:%M %p}'
    if order.start_time:
        return day, f'From {order.start_time:%I:%M %p}'
    return day, 'See event page for timings'


def _place(order):
    from .notifications import _venue_text
    return _venue_text(order) or order.location or ''


def _poster_allowed(url):
    """Only our own image storage (Supabase) over https — never an arbitrary
    host from a stored URL."""
    parsed = urlparse(str(url or ''))
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    allowed = set(getattr(settings, 'TICKET_POSTER_HOSTS', ()) or ())
    supabase = urlparse(getattr(settings, 'SUPABASE_URL', '') or '').hostname
    if supabase:
        allowed.add(supabase)
    return parsed.hostname in allowed


def fetch_poster(url):
    """An ImageReader for the event poster, or None (skipped quietly)."""
    if not url or not _poster_allowed(url):
        return None
    try:
        import requests
        from PIL import Image
        from reportlab.lib.utils import ImageReader

        with requests.get(url, timeout=POSTER_TIMEOUT, stream=True) as response:
            if response.status_code != 200:
                return None
            data = response.raw.read(POSTER_MAX_BYTES + 1, decode_content=True)
        if len(data) > POSTER_MAX_BYTES:
            return None
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        image.thumbnail((1200, 1200))
        return ImageReader(image)
    except Exception as exc:                 # never fail a ticket for a picture
        logger.info('Ticket poster skipped: %s', exc)
        return None


def _wrap(canvas, text, font, size, width):
    """Greedy word wrap for one paragraph."""
    words, lines, line = str(text or '').split(), [], ''
    for word in words:
        trial = f'{line} {word}'.strip()
        if canvas.stringWidth(trial, font, size) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or ['']


def _logo(c, x, y):
    """The HAPVOO wordmark as text, white on the header band. (The official
    logo is black artwork that would vanish here; the old "B" mark is gone.)"""
    from reportlab.lib.colors import white

    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 20)
    c.drawString(x, y + 8, BRAND)


def _qr(c, payload, x, y, size):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing

    widget = qr.QrCodeWidget(payload, barLevel='M')
    x0, y0, x1, y1 = widget.getBounds()
    w, h = x1 - x0, y1 - y0
    drawing = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x, y)


def render(order, *, poster=True, now=None, compress=True):
    """The ticket PDF for a verified-paid order, as bytes."""
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as canvas_mod

    rows = tickets_mod.tickets_for(order)
    if not rows:
        raise TicketUnavailable('This order has no valid tickets.')

    image = fetch_poster(order.image or getattr(order.event, 'poster_url', '')) if poster else None
    generated = (now or datetime.datetime.now(IST)).astimezone(IST)
    day, times = _when(order)
    place = _place(order)
    width, height = A4
    margin = 40
    inner = width - 2 * margin

    buffer = io.BytesIO()
    c = canvas_mod.Canvas(buffer, pagesize=A4, pageCompression=1 if compress else 0)
    c.setTitle(f'Tickets {order.id} - {order.event_name}')
    c.setAuthor(BRAND)
    c.setSubject(f'{len(rows)} ticket(s) for {order.event_name}')

    for ticket in rows:
        # Header band
        c.setFillColor(HexColor(NAVY))
        c.rect(0, height - 92, width, 92, stroke=0, fill=1)
        _logo(c, margin, height - 62)
        c.setFillColor(white)
        c.setFont('Helvetica-Bold', 11)
        c.drawRightString(width - margin, height - 46, 'EVENT TICKET')
        c.setFont('Helvetica', 10)
        c.drawRightString(width - margin, height - 61,
                          f'Ticket {ticket["number"]} of {ticket["total"]}')

        y = height - 92 - 28
        # Poster (optional) on the left, title block beside it
        text_x, text_w = margin, inner
        if image is not None:
            iw, ih = image.getSize()
            box_w, box_h = 150, 190
            scale = min(box_w / iw, box_h / ih)
            dw, dh = iw * scale, ih * scale
            c.drawImage(image, margin, y - dh, dw, dh, preserveAspectRatio=True, mask='auto')
            text_x, text_w = margin + box_w + 20, inner - box_w - 20

        c.setFillColor(HexColor(NAVY))
        title_lines = _wrap(c, order.event_name or 'Event', 'Helvetica-Bold', 22, text_w)[:3]
        ty = y - 20
        c.setFont('Helvetica-Bold', 22)
        for line in title_lines:
            c.drawString(text_x, ty, line)
            ty -= 27
        c.setFillColor(HexColor(MUTED))
        c.setFont('Helvetica', 12)
        c.drawString(text_x, ty, day)
        ty -= 17
        c.drawString(text_x, ty, times)
        ty -= 17
        for line in _wrap(c, place, 'Helvetica', 12, text_w)[:3]:
            c.drawString(text_x, ty, line)
            ty -= 16

        y = min(ty, y - (190 if image is not None else 0)) - 26

        # Details table
        details = [
            ('Ticket holder', order.customer_name or '-'),
            ('Booking / order ID', order.id),
            ('Ticket ID', ticket['ticketId']),
            ('Ticket type', ticket['typeLabel'] + (
                f' (admits {ticket["seats"]})' if ticket['seats'] > 1 else '')),
            ('Ticket price', _rupees(ticket['price'])),
            ('Tickets in this order', str(order.ticket_count or ticket['total'])),
            ('Amount paid (order)', _rupees(order.amount)),
            ('Booking fee (included)', _rupees(order.fee)),
            ('Invoice', order.invoice_number or '-'),
        ]
        c.setStrokeColor(HexColor(LINE))
        c.setLineWidth(1)
        c.line(margin, y + 14, width - margin, y + 14)
        label_w = 150
        details_w = inner - 190
        table_top = y
        for label, value in details:
            c.setFillColor(HexColor(MUTED))
            c.setFont('Helvetica', 10.5)
            c.drawString(margin, y, label)
            c.setFillColor(HexColor(NAVY))
            c.setFont('Helvetica-Bold', 11.5)
            lines = _wrap(c, value, 'Helvetica-Bold', 11.5, details_w - label_w)[:2]
            for i, line in enumerate(lines):
                c.drawString(margin + label_w, y - 14 * i, line)
            y -= 24 + 14 * (len(lines) - 1)

        # QR + verification code, right column beside the table
        qr_size = 170
        qr_x = width - margin - qr_size
        qr_y = table_top - qr_size + 6
        _qr(c, ticket['qr'], qr_x, qr_y, qr_size)
        c.setFillColor(HexColor(NAVY))
        c.setFont('Helvetica-Bold', 13)
        code = ticket['code']
        c.drawCentredString(qr_x + qr_size / 2, qr_y - 16, f'{code[:4]}-{code[4:]}')
        c.setFillColor(HexColor(MUTED))
        c.setFont('Helvetica', 9)
        c.drawCentredString(qr_x + qr_size / 2, qr_y - 29, 'Verification code')

        # Footer
        c.setStrokeColor(HexColor(LINE))
        c.line(margin, 118, width - margin, 118)
        c.setFillColor(HexColor(MUTED))
        c.setFont('Helvetica', 9.5)
        notes = [
            'Show this QR code (or the Booking ID) at the entrance. Valid only for the date shown.',
            f'This order has {ticket["total"]} ticket(s); checking in the order admits the whole party together.',
            'Tickets from cancelled or refunded orders are not valid. Do not share this ticket.',
        ]
        ny = 100
        for note in notes:
            c.drawString(margin, ny, note)
            ny -= 14
        c.setFont('Helvetica', 8.5)
        c.drawString(margin, 46, f'{BRAND} · Generated {generated:%d %b %Y, %I:%M %p} IST')
        c.drawRightString(width - margin, 46, ticket['ticketId'])
        c.showPage()

    c.save()
    return buffer.getvalue()


def filename(order):
    return f'tickets-{order.id}.pdf'
