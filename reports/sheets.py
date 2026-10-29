"""
Sheets more than one export writes: the event sales sheets (super admin and
vendor) and the venue booking row (vendor Bookings and Earnings exports).
"""
from . import common as c
from .workbook import DATE, DATETIME, INT, MONEY, Sheet, col

# --- events ----------------------------------------------------------


def _first_last(event):
    dates = [d.date for d in event.dates.all()]
    return (min(dates), max(dates)) if dates else (None, None)


def event_sheets(events, *, owner=None):
    """Events, Ticket Types and Dates sheets for `events` (already scoped
    and ordered by the caller).

    owner=None is the super admin's version, with the columns only an admin
    sees (submitting vendor and organiser phones, promotion). Passing the
    vendor writes the vendor's version, where each event says whether the
    vendor submitted it or it is a published event at one of their venues."""
    admin = owner is None
    events = list(events)
    stats, by_date = c.event_sales(e.id for e in events)

    def sales(event):
        return stats.get(str(event.id), c.EMPTY_SALES)

    columns = [
        col('Event ID', 38),
        col('Event Name', 30),
        col('Category', 16),
        col('Approval Status', 17),
    ]
    if admin:
        columns += [
            col('Promoted on Homepage', 12),
            col('Managed By', 12),
            col('Submitting Vendor', 22),
            col('Vendor Phone', 14),
            col('Organiser', 22),
            col('Organiser Phone', 15),
        ]
    else:
        columns += [col('Submitted By', 26)]
    columns += [
        col('Venue', 26),
        col('Location', 32),
        col('City', 14),
        col('Start Time', 10),
        col('End Time', 10),
        col('First Date', 13, DATE),
        col('Last Date', 13, DATE),
        col('Dates', 8, INT),
        col('Total Capacity', 12, INT),
        col('Seats Sold (paid)', 12, INT),
        col('Tickets Sold', 11, INT),
        col('Paid Orders', 11, INT),
        col('Ticket Revenue (excl. fee)', 16, MONEY),
        col('Platform Fees on Paid Orders', 16, MONEY),
        col('Refund Pending Orders', 12, INT),
        col('Refund Pending Amount', 14, MONEY),
        col('Refunded Orders', 11, INT),
        col('Refunded Amount', 14, MONEY),
        col('From Price', 11, MONEY),
        col('Booking Opens', 13, DATE),
        col('Booking Closes', 13, DATE),
        col('Review Notes', 34),
        col('Submitted', 18, DATETIME),
    ]

    def event_row(event):
        s = sales(event)
        first, last = _first_last(event)
        prices = [t.price for t in event.ticket_types.all() if t.is_active]
        seats = sum(by_date.get(d.id, (0, 0))[0] for d in event.dates.all())
        row = [
            str(event.id),
            event.name,
            event.category,
            c.EVENT_LIFECYCLE_LABEL.get(event.status, event.status),
        ]
        vendor_name = (event.vendor.name or event.vendor.phone) if event.vendor_id else ''
        if admin:
            row += [
                event.featured and event.status == 'live',
                'Vendor' if event.vendor_id else 'Admin',
                vendor_name,
                event.vendor.phone if event.vendor_id else '',
                event.organizer_name,
                event.organizer_phone,
            ]
        else:
            row += ['You' if event.vendor_id == owner.id else
                    'TheBookMyVenues (at your venue)']
        row += [
            event.venue_name,
            event.location,
            event.city,
            c.hhmm(event.start_time),
            c.hhmm(event.end_time),
            first,
            last,
            len(event.dates.all()),
            sum(d.capacity for d in event.dates.all()),
            seats,
            s['tickets'],
            s['orders'],
            s['revenue'],
            s['fees'],
            s['refund_pending'][0],
            s['refund_pending'][1],
            s['refunded'][0],
            s['refunded'][1],
            min(prices) if prices else None,
            event.booking_opens_on,
            event.booking_closes_on,
            event.review_notes,
            event.created_at,
        ]
        return row

    def ticket_rows():
        for event in events:
            by_type = sales(event)['by_type']
            for ticket in event.ticket_types.all():
                qty, revenue = by_type.get(ticket.key, (0, 0))
                yield [
                    str(event.id), event.name, ticket.label, ticket.key,
                    ticket.price, ticket.seats_per_ticket,
                    ticket.max_per_order or None, ticket.is_active, qty, revenue,
                ]

    def date_rows():
        for event in events:
            for event_date in event.dates.all():
                paid, held = by_date.get(event_date.id, (0, 0))
                yield [
                    str(event.id), event.name, event_date.date, event_date.capacity,
                    paid, held, max(0, event_date.capacity - paid - held),
                    event_date.is_available,
                ]

    return [
        Sheet('Events', columns, [event_row(e) for e in events]),
        Sheet('Ticket Types', [
            col('Event ID', 38), col('Event Name', 30), col('Ticket Type', 18),
            col('Key', 12), col('Price', 11, MONEY), col('Seats per Ticket', 10, INT),
            col('Max per Order', 10, INT), col('Active', 8),
            col('Tickets Sold (paid)', 12, INT), col('Ticket Revenue', 14, MONEY),
        ], list(ticket_rows())),
        Sheet('Dates', [
            col('Event ID', 38), col('Event Name', 30), col('Date', 13, DATE),
            col('Capacity', 10, INT), col('Seats Sold (paid)', 12, INT),
            col('Seats Held in Checkout', 13, INT), col('Remaining', 10, INT),
            col('Open for Sale', 10),
        ], list(date_rows())),
    ]


# --- venue bookings (vendor) -----------------------------------------

VENDOR_BOOKING_COLUMNS = [
    col('Booking ID', 16),
    col('Venue', 28),
    col('Pitch / Unit', 16),
    col('Sport', 14),
    col('Customer Name', 22),
    col('Customer Phone', 15),
    col('Booking Date', 13, DATE),
    col('Time Slots', 28),
    col('Channel', 10),
    col('Payment Method', 14),
    col('Payment Status', 20),
    col('Booking Status', 15),
    col('Booking Amount', 14, MONEY),
    col('Platform Fee', 12, MONEY),
    col('Discount', 11, MONEY),
    col('Discount Funded By', 12),
    col('Your Earnings', 14, MONEY),
    col('Paid Online', 13, MONEY),
    col('Collect at Venue', 13, MONEY),
    col('Cash Collected', 10),
    col('Refund Status', 14),
    col('Refund Amount', 13, MONEY),
    col('Add-ons', 24),
    col('Occasion', 16),
    col('Occasion Note', 28),
    col('Checked In At', 18, DATETIME),
    col('Booked On', 18, DATETIME),
]

VENDOR_BOOKING_STATUS = {
    'confirmed': 'Upcoming',
    'completed': 'Completed',
    'refund_pending': 'Refund pending',
    'refunded': 'Refunded',
}


def online_share(booking):
    """The part of the bill paid through the platform (0 when the whole bill
    is settled at the venue) — the split the vendor's booking bill shows."""
    if c.is_walk_in(booking) or booking.method == 'venue':
        return 0
    return booking.online_amount


def vendor_booking_row(booking, *, show_phone=True):
    """One venue booking as its vendor sees it. `show_phone=False` blanks the
    customer's number on rows the vendor dashboard does not list (refunded
    bookings), so an export never shows more than the dashboard does."""
    status = booking.display_status
    kept = c.is_kept(booking.status)
    cash_due = c.venue_cash_due(booking) if kept else 0
    refunded, _basis = c.refund_figures(
        booking.status, booking.refund_amount, c.venue_captured(booking)
    )
    return [
        booking.id,
        booking.venue_name.split(' — ')[0] if booking.venue_name else '',
        booking.unit_label or (
            booking.venue_name.split(' — ', 1)[1] if ' — ' in (booking.venue_name or '') else ''
        ),
        booking.sport,
        booking.customer_name,
        booking.phone if show_phone else '',
        booking.date,
        ', '.join(booking.slots or []),
        'Walk-in' if c.is_walk_in(booking) else 'Platform',
        c.method_label(booking.method),
        c.payment_status(booking),
        VENDOR_BOOKING_STATUS.get(status, c.VENUE_STATUS_LABEL.get(status, status)),
        booking.amount,
        booking.fee,
        booking.discount_amount or None,
        c.offer_funding(booking),
        c.vendor_net(booking) if kept else 0,
        online_share(booking),
        cash_due or None,
        (booking.collected if cash_due else ''),
        c.REFUND_STATUS_LABEL.get(booking.status, ''),
        refunded,
        c.addons_text(booking.addons),
        booking.occasion,
        booking.occasion_note,
        booking.checked_in_at,
        booking.created_at,
    ]
