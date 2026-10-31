"""
Super-admin routes — mounted at /api/admin/ (separate base from the
customer/vendor app, cookie-session auth, {"detail"} error shape).
"""
from django.urls import path

from campaigns import admin_views as campaign_views
from events import admin_views as event_admin_views
from reports.views import AdminExportView

from . import data_review, homepage, ratings_admin, subcategories_admin, venue_edit, views

urlpatterns = [
    path('auth/login', views.AdminLoginView.as_view(), name='admin-login'),
    path('auth/verify-otp', views.AdminVerifyOtpView.as_view(), name='admin-verify-otp'),
    path('auth/logout', views.AdminLogoutView.as_view(), name='admin-logout'),
    path('bootstrap', views.AdminBootstrapView.as_view(), name='admin-bootstrap'),
    # Writes (Phase 2)
    path('approvals/<uuid:listing_id>', views.AdminApprovalUpdateView.as_view(), name='admin-approval'),
    path('venues/<uuid:listing_id>', views.AdminVenueUpdateView.as_view(), name='admin-venue'),
    # Read-only list of records that look like test data (adminpanel/data_review.py).
    path('data-review', data_review.AdminDataReviewView.as_view(), name='admin-data-review'),
    # All venues → Edit: every vendor-submitted detail except bank details.
    path('venues/<uuid:listing_id>/edit', venue_edit.AdminVenueEditView.as_view(), name='admin-venue-edit'),
    # Must precede the <int:vendor_id> route so 'token' is never read as an id.
    path('vendors/token', views.AdminVendorTokenView.as_view(), name='admin-vendor-token'),
    path('vendors/<int:vendor_id>', views.AdminVendorUpdateView.as_view(), name='admin-vendor'),
    path('users/<int:user_id>', views.AdminUserUpdateView.as_view(), name='admin-user'),
    # Must precede bookings/<str:booking_id> so 'export' is never read as an id.
    path('bookings/export', views.AdminBookingsExportView.as_view(), name='admin-bookings-export'),
    path('bookings/<str:booking_id>', views.AdminBookingUpdateView.as_view(), name='admin-booking'),
    # Excel exports of the other panel pages (users, vendors, venues,
    # approvals, events, payouts, refunds, payments report, audit log) —
    # read-only, audited. See reports/admin_reports.py.
    path('exports/<slug:kind>', AdminExportView.as_view(), name='admin-export'),
    # Booking ID verification across all venues/events — read-only.
    path('check-ins/verify', views.AdminBookingVerifyView.as_view(), name='admin-check-in-verify'),
    # Ticketed events — the SUPER ADMIN owns these outright (create, edit,
    # publish/unpublish, delete). Vendors have no write path; see
    # events/vendor_views.py. Static 'events' route first so the list/create
    # endpoint is never read as an event id.
    path('events', event_admin_views.AdminEventsView.as_view(), name='admin-events'),
    path(
        'events/<uuid:event_id>',
        event_admin_views.AdminEventDetailView.as_view(),
        name='admin-event',
    ),
    path(
        'events/<uuid:event_id>/orders',
        event_admin_views.AdminEventOrdersView.as_view(),
        name='admin-event-orders',
    ),
    path(
        'events/<uuid:event_id>/orders/<str:order_id>',
        event_admin_views.AdminEventOrderDetailView.as_view(),
        name='admin-event-order',
    ),
    # Remove an unpaid event order from the main Bookings page — keyed on
    # the order alone, so it works after its event was deleted.
    path(
        'event-orders/<str:order_id>',
        event_admin_views.AdminEventOrderRemoveView.as_view(),
        name='admin-event-order-remove',
    ),
    # New models (Phase 3)
    path('uploads', views.AdminUploadView.as_view(), name='admin-upload'),
    path('settings', views.AdminSettingsView.as_view(), name='admin-settings'),
    path('payouts/<int:payout_id>', views.AdminPayoutUpdateView.as_view(), name='admin-payout'),
    path('reviews/<int:review_id>/resolve', views.AdminReviewResolveView.as_view(), name='admin-review-resolve'),
    path('audit', views.AdminAuditView.as_view(), name='admin-audit'),
    # Customer ratings & reviews — moderation only (adminpanel/ratings_admin.py).
    # Custom sub-categories added via the wizards' "Others" option — approve /
    # reject / add (adminpanel/subcategories_admin.py).
    path('subcategories', subcategories_admin.AdminSubCategoriesView.as_view(), name='admin-subcategories'),
    path(
        'subcategories/<int:subcategory_id>',
        subcategories_admin.AdminSubCategoryDetailView.as_view(),
        name='admin-subcategory',
    ),
    path('ratings', ratings_admin.AdminRatingsView.as_view(), name='admin-ratings'),
    path('ratings/<int:rating_id>', ratings_admin.AdminRatingDetailView.as_view(), name='admin-rating'),
    # The Explore homepage: banners & promotions, featured events and venues,
    # section switches/order (adminpanel/homepage.py). Static segments are
    # declared before the <id> routes they would otherwise be read as.
    path('homepage', homepage.AdminHomepageView.as_view(), name='admin-homepage'),
    path('homepage/sections', homepage.AdminHomepageSectionsView.as_view(), name='admin-homepage-sections'),
    path('homepage/banner-image', homepage.AdminBannerImageView.as_view(), name='admin-homepage-banner-image'),
    path('homepage/banners', homepage.AdminBannersView.as_view(), name='admin-homepage-banners'),
    path('homepage/banners/reorder', homepage.AdminBannersReorderView.as_view(), name='admin-homepage-banners-reorder'),
    path('homepage/banners/<str:banner_ref>', homepage.AdminBannerDetailView.as_view(), name='admin-homepage-banner'),
    path('homepage/featured-events', homepage.AdminFeaturedEventsView.as_view(), name='admin-homepage-featured-events'),
    path(
        'homepage/featured-events/reorder',
        homepage.AdminFeaturedEventsReorderView.as_view(),
        name='admin-homepage-featured-events-reorder',
    ),
    path(
        'homepage/featured-events/<uuid:event_id>',
        homepage.AdminFeaturedEventDetailView.as_view(),
        name='admin-homepage-featured-event',
    ),
    path(
        'homepage/featured-venues/reorder',
        homepage.AdminFeaturedVenuesReorderView.as_view(),
        name='admin-homepage-featured-venues-reorder',
    ),    # Email Campaigns (campaigns/admin_views.py). Static segments before the
    # <uuid> routes they would otherwise be read as.
    path('campaigns', campaign_views.CampaignsView.as_view(), name='admin-campaigns'),
    path('campaigns/options', campaign_views.CampaignOptionsView.as_view(), name='admin-campaign-options'),
    path('campaigns/audience', campaign_views.CampaignAudienceView.as_view(), name='admin-campaign-audience'),
    path('campaigns/audience/recipients', campaign_views.CampaignAudienceRecipientsView.as_view(),
         name='admin-campaign-audience-recipients'),
    path('campaigns/preview', campaign_views.CampaignPreviewView.as_view(), name='admin-campaign-preview'),
    path('campaigns/images', campaign_views.CampaignImagesView.as_view(), name='admin-campaign-images'),
    path('campaigns/<uuid:campaign_id>', campaign_views.CampaignDetailView.as_view(), name='admin-campaign'),
    path('campaigns/<uuid:campaign_id>/duplicate', campaign_views.CampaignDuplicateView.as_view(),
         name='admin-campaign-duplicate'),
    path('campaigns/<uuid:campaign_id>/test', campaign_views.CampaignTestView.as_view(), name='admin-campaign-test'),
    path('campaigns/<uuid:campaign_id>/send', campaign_views.CampaignSendView.as_view(), name='admin-campaign-send'),
    path('campaigns/<uuid:campaign_id>/cancel', campaign_views.CampaignCancelView.as_view(),
         name='admin-campaign-cancel'),
    path('campaigns/<uuid:campaign_id>/recipients', campaign_views.CampaignRecipientsView.as_view(),
         name='admin-campaign-recipients'),
]
