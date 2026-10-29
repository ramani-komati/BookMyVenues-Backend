"""
Super-admin moderation of customer ratings & reviews (Reviews page).

    GET   /api/admin/ratings?kind=&visibility=&written=&q=&limit=&offset=
    PATCH /api/admin/ratings/<id>   {hidden: true|false, reason}
                                    {replyHidden: true|false, reason}

Ratings are genuine customer submissions (bookings/ratings.py), so the admin
can never write, edit or invent one — only HIDE one (abuse, spam, personal
details) and restore it. A hidden rating stays in the table, leaves every
average, count and public review list, and the change is audited with its
reason. The flagged-report queue (adminpanel.Review) is untouched.
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from bookings.models import Rating
from bookings.ratings import ratings_changed, venue_set_listings

from .auth import detail
from .views import _AdminWriteView, record_audit

MAX_LIMIT = 100


def rating_row(rating):
    is_event = rating.event_order_id is not None
    if is_event:
        order = rating.event_order
        target = (rating.event.name if rating.event_id and rating.event else '') or \
            (order.event_name if order else '')
        booking_id = rating.event_order_id
        booking_date = order.date.isoformat() if order else None
    else:
        booking = rating.booking
        target = (rating.listing.name if rating.listing_id and rating.listing else '') or \
            (booking.venue_name if booking else '')
        booking_id = rating.booking_id
        booking_date = booking.date.isoformat() if booking else None
    user = rating.user
    return {
        'id': rating.pk,
        'kind': 'event' if is_event else 'venue',
        'targetId': str(rating.event_id if is_event else rating.listing_id or ''),
        'targetName': target,
        'bookingId': booking_id,
        'bookingDate': booking_date,
        'stars': rating.stars,
        'review': rating.review,
        'customerName': (getattr(user, 'name', '') or '') if user else '',
        'createdAt': rating.created_at.isoformat(),
        'hidden': rating.hidden_at is not None,
        'hiddenAt': rating.hidden_at.isoformat() if rating.hidden_at else None,
        'hiddenReason': rating.hidden_reason,
        # The venue / event owner's public reply and its moderation state.
        'reply': rating.reply or None,
        'repliedAt': rating.replied_at.isoformat() if rating.replied_at else None,
        'replyHidden': rating.reply_hidden_at is not None,
        'replyHiddenReason': rating.reply_hidden_reason or None,
    }


class AdminRatingsView(_AdminWriteView):
    """The ratings list, newest first, filtered on the server."""

    def get(self, request):
        params = request.query_params
        queryset = Rating.objects.select_related(
            'listing', 'booking', 'event', 'event_order', 'user',
        ).order_by('-created_at', '-id')

        kind = params.get('kind', 'all')
        if kind == 'venue':
            queryset = queryset.filter(booking__isnull=False)
        elif kind == 'event':
            queryset = queryset.filter(event_order__isnull=False)
        elif kind != 'all':
            return detail('kind must be venue, event or all.', status.HTTP_400_BAD_REQUEST)

        visibility = params.get('visibility', 'all')
        if visibility == 'visible':
            queryset = queryset.filter(hidden_at__isnull=True)
        elif visibility == 'hidden':
            queryset = queryset.filter(hidden_at__isnull=False)
        elif visibility != 'all':
            return detail('visibility must be visible, hidden or all.', status.HTTP_400_BAD_REQUEST)

        if params.get('written') in ('1', 'true'):
            queryset = queryset.exclude(review='')

        q = str(params.get('q') or '').strip()
        if q:
            queryset = queryset.filter(
                Q(review__icontains=q) | Q(listing__name__icontains=q)
                | Q(event__name__icontains=q) | Q(user__name__icontains=q)
                | Q(booking__id__iexact=q) | Q(event_order__id__iexact=q)
            )

        try:
            limit = int(params.get('limit', 50))
            offset = int(params.get('offset', 0))
        except (TypeError, ValueError):
            return detail('limit and offset must be numbers.', status.HTTP_400_BAD_REQUEST)
        if not 1 <= limit <= MAX_LIMIT or offset < 0:
            return detail('Invalid limit or offset.', status.HTTP_400_BAD_REQUEST)

        total = queryset.count()
        return Response({
            'ratings': [rating_row(r) for r in queryset[offset:offset + limit]],
            'total': total,
        })


class AdminRatingDetailView(_AdminWriteView):
    """PATCH {hidden, reason} — hide (reason required) or restore."""

    def patch(self, request, rating_id):
        rating = Rating.objects.select_related(
            'listing', 'booking', 'event', 'event_order', 'user',
        ).filter(pk=rating_id).first()
        if rating is None:
            return detail('Rating not found.', status.HTTP_404_NOT_FOUND)
        body = self._body(request)
        if 'replyHidden' in body:
            return self._moderate_reply(request, rating, body)
        hidden = body.get('hidden')
        if not isinstance(hidden, bool):
            return detail('hidden must be true or false.', status.HTTP_400_BAD_REQUEST)
        reason = str(body.get('reason') or '').strip()[:200]
        if hidden and not reason:
            return detail('Give a reason for hiding this review.', status.HTTP_400_BAD_REQUEST)

        was_hidden = rating.hidden_at is not None
        if hidden != was_hidden:
            rating.hidden_at = timezone.now() if hidden else None
            rating.hidden_reason = reason if hidden else ''
            rating.save(update_fields=['hidden_at', 'hidden_reason', 'updated_at'])
            # Averages, counts and review lists change with it.
            ratings_changed(
                venue_set_listings(rating.listing) if rating.listing_id else (),
                event_id=rating.event_id,
            )
            row = rating_row(rating)
            record_audit(
                request,
                'Hid customer review' if hidden else 'Restored customer review',
                row['targetName'] or 'Rating',
                f'{rating.stars}★ by {row["customerName"] or "customer"}',
                target_id=str(rating.pk),
                reason=reason if hidden else None,
            )
        return Response(rating_row(rating))

    def _moderate_reply(self, request, rating, body):
        """Hide (reason required) or restore the vendor's reply. The
        vendor cannot rewrite a hidden reply; they may delete it."""
        hidden = body.get('replyHidden')
        if not isinstance(hidden, bool):
            return detail('replyHidden must be true or false.', status.HTTP_400_BAD_REQUEST)
        if not rating.reply:
            return detail('This review has no reply.', status.HTTP_400_BAD_REQUEST)
        reason = str(body.get('reason') or '').strip()[:200]
        if hidden and not reason:
            return detail('Give a reason for hiding this reply.', status.HTTP_400_BAD_REQUEST)
        if hidden != (rating.reply_hidden_at is not None):
            rating.reply_hidden_at = timezone.now() if hidden else None
            rating.reply_hidden_reason = reason if hidden else ''
            rating.save(update_fields=['reply_hidden_at', 'reply_hidden_reason', 'updated_at'])
            row = rating_row(rating)
            record_audit(
                request,
                'Hid vendor reply' if hidden else 'Restored vendor reply',
                row['targetName'] or 'Rating',
                f'reply to {rating.stars}★ by {row["customerName"] or "customer"}',
                target_id=str(rating.pk),
                reason=reason if hidden else None,
            )
        return Response(rating_row(rating))
