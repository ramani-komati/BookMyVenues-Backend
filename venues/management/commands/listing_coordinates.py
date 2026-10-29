"""
Report — and optionally fill — venue map pins for distance search.

    python manage.py listing_coordinates
        Dry run. Lists every non-deleted venue and event without a pin, and
        why (no maps link / short link / link without coordinates).

    python manage.py listing_coordinates --resolve-short-links
        Also follows each SHORT share link (maps.app.goo.gl …) through the
        existing SSRF-guarded resolver (venues/maps.py — Google short-link
        hosts only) and shows what pin it would yield. Still writes nothing.

    python manage.py listing_coordinates --resolve-short-links --apply
        Stores the resolved URL beside the vendor's link as
        detail.mapsLinkResolved = {source, url} and saves the listing, which
        re-derives latitude/longitude. The vendor's own mapsLink, address
        and every other field are left exactly as they were.

Venues with no link, or a link without a pin, cannot be fixed here — the
vendor has to paste a precise link (the wizard explains how). Nothing is
ever guessed from a name or an address.
"""
from django.core.management.base import BaseCommand

from events.models import Event
from venues.geo import pin_from_maps_link, record_maps_link, record_pin
from venues.maps import MapsError, _is_allowed, resolve_short_link
from venues.models import Listing


class Command(BaseCommand):
    help = 'Report venue/event map-pin coverage; optionally resolve short maps links.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--resolve-short-links', action='store_true',
            help='Follow short share links through the existing resolver (network).',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='With --resolve-short-links: save the resolved pins.',
        )

    def handle(self, *args, resolve_short_links=False, apply=False, **options):
        if apply and not resolve_short_links:
            self.stderr.write('--apply only works together with --resolve-short-links.')
            return

        listings = Listing.objects.exclude(status=Listing.Status.DELETED).order_by('name')
        total = pinned = fixed = 0
        for listing in listings:
            total += 1
            if listing.latitude is not None and listing.longitude is not None:
                pinned += 1
                continue
            link = record_maps_link(listing.record)
            reason = (
                'no maps link' if not link
                else 'short link (no pin until resolved)' if _is_allowed(link)
                else 'link has no exact pin'
            )
            line = f'  {listing.name or listing.pk} [{listing.status}] — {reason}'
            if resolve_short_links and _is_allowed(link):
                line += self._resolve(listing, link, apply)
                if apply and listing.latitude is not None:
                    fixed += 1
            self.stdout.write(line)

        self.stdout.write(
            f'Venues: {pinned}/{total} have a map pin'
            + (f'; {fixed} filled from short links.' if apply else '.')
        )

        events = Event.objects.exclude(status=Event.Status.DELETED).order_by('name')
        missing = [e for e in events if e.latitude is None or e.longitude is None]
        for event in missing:
            why = 'no maps link' if not event.maps_link else (
                'link has no exact pin' if pin_from_maps_link(event.maps_link) is None else '?'
            )
            self.stdout.write(f'  event {event.name} [{event.status}] — {why}')
        self.stdout.write(f'Events: {events.count() - len(missing)}/{events.count()} have a map pin.')
        if not apply:
            self.stdout.write('Dry run — nothing was changed.')

    def _resolve(self, listing, link, apply):
        try:
            full = resolve_short_link(link)
        except MapsError as error:
            return f' → could not resolve ({error})'
        point = pin_from_maps_link(full)
        if point is None:
            return ' → resolved, but the full link has no exact pin either'
        if not apply:
            return f' → would pin at {point[0]:.6f}, {point[1]:.6f}'
        record = dict(listing.record or {})
        detail = dict(record.get('detail') or {})
        detail['mapsLinkResolved'] = {'source': link, 'url': full}
        record['detail'] = detail
        if record_pin(record) is None:   # defensive: never save a no-op
            return ' → resolved, but no pin could be read'
        listing.record = record
        # Listing.save adds latitude/longitude to update_fields; updated_at is
        # left alone so the venue does not jump the "popular" sort.
        listing.save(update_fields=['record'])
        from venues.public_views import invalidate_listing_cache
        invalidate_listing_cache(listing)
        return f' → pinned at {listing.latitude:.6f}, {listing.longitude:.6f}'
