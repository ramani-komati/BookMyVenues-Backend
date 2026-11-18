"""
The fixed option lists behind the Fitness, Resort and Big Screen Surprise
registration forms.

The vendor wizard and the super admin's register / edit forms carry the SAME
lists (src/features/venue-registration/offerings/model.js in both apps — diff
them); this module is what the server validates against. A vendor-typed
"Other" value is always allowed alongside them, so nothing a vendor needs is
ever refused for not being on a list.
"""

# The three venue types that sell something other than hourly slots. Keys are
# the wizard's stored venue type (details.primaryCategory); values the record
# block that carries their configuration (record.detail.<block>).
OFFERING_TYPES = {
    'Gym': 'fitness',
    'Resort': 'resort',
    'Big Screen Surprise': 'surprise',
}

WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

# --- Fitness -----------------------------------------------------------------

FITNESS_SERVICES = (
    'General Gym / Strength Training', 'Cardio Training', 'Personal Training',
    'Weight Loss Programs', 'Yoga', 'Zumba', 'CrossFit / Functional Training',
    'Pilates', 'Group Fitness Classes', 'Martial Arts / Boxing',
    'Dance Fitness', 'Physiotherapy / Recovery',
)

FITNESS_AMENITIES = (
    'Air Conditioning', 'Locker Room', 'Changing Rooms', 'Shower Facilities',
    'Parking', 'Drinking Water', 'Personal Trainer', 'Steam / Sauna',
    'Swimming Pool', 'Ladies-Only Area', 'Accessible Entrance', 'CCTV / Security',
)

# Membership plan kinds and the validity each starts with in the form. The
# vendor can change every value; `custom` is a plan of their own.
PLAN_KINDS = {
    'day_pass': {'name': 'Single Day Pass', 'value': 1, 'unit': 'day'},
    'weekly': {'name': 'Weekly Membership', 'value': 1, 'unit': 'week'},
    'monthly': {'name': 'Monthly Membership', 'value': 1, 'unit': 'month'},
    'quarterly': {'name': 'Quarterly Membership (3 Months)', 'value': 3, 'unit': 'month'},
    'half_yearly': {'name': 'Half-Yearly Membership (6 Months)', 'value': 6, 'unit': 'month'},
    'annual': {'name': 'Annual Membership (12 Months)', 'value': 12, 'unit': 'month'},
    'personal_training': {'name': 'Personal Training', 'value': 1, 'unit': 'month'},
    'group_class': {'name': 'Yoga / Zumba / Group Classes', 'value': 1, 'unit': 'month'},
    'custom': {'name': 'Custom plan', 'value': 1, 'unit': 'month'},
}
DURATION_UNITS = ('day', 'week', 'month')

# When a membership starts: on the purchase date, or on a date the customer
# picks (within `startWithinDays` of buying it).
START_RULES = ('purchase', 'chosen')
# When it ends: after its duration, or when its visits run out if that comes
# first (only meaningful with a visit limit).
EXPIRY_RULES = ('duration', 'visits_or_duration')
MAX_START_WITHIN_DAYS = 90

# --- Resort ------------------------------------------------------------------

RESORT_TYPES = (
    'Resort', 'Farm Resort', 'Luxury Resort', 'Family Resort', 'Couple Resort',
    'Adventure Resort', 'Water Resort', 'Other',
)

RESORT_FACILITIES = {
    'poolPlay': (
        'Swimming Pool', 'Kids Pool', 'Water Games', 'Indoor Games',
        'Outdoor Games', 'Badminton', 'Cricket',
    ),
    'food': (
        'Restaurant', 'Buffet', 'Breakfast', 'Lunch', 'Dinner', 'Snacks',
        'Bar / Drinks (where legally permitted)', 'Outside Food Allowed',
    ),
    'other': (
        'Parking', 'Wi-Fi', 'AC Rooms', 'Conference Hall', 'Party Hall',
        'Campfire', 'DJ / Music', 'Bonfire', 'Garden', 'Photography Area',
    ),
}

RESORT_INCLUSIONS = (
    'Swimming Pool', 'Breakfast', 'Lunch', 'Snacks', 'Indoor Games',
    'Outdoor Games', 'Kids Activities',
)

# day = a Half Day / Full Day outing package; pool = Swimming Pool / Water
# Park entry sold per time slot with adult and child prices.
PACKAGE_KINDS = ('day', 'pool')
PACKAGE_DURATIONS = ('half_day', 'full_day')
# per_person: price × adults + child price × children
# per_package: one price for the group (within min–max persons)
# per_hour: per-person price for every hour of the chosen slot
PACKAGE_PRICING = ('per_person', 'per_package', 'per_hour')

# --- Big Screen Surprise -----------------------------------------------------

CINEMA_BRANDS = ('PVR', 'Asian Cinemas', 'Local Theatre', 'Other')
THEATRE_TYPES = ('Multiplex', 'Single Screen')
SCREEN_FORMATS = ('2D', '3D', 'IMAX', '35mm', '70mm', 'Other')
SURPRISE_OCCASIONS = (
    'Birthday', 'Anniversary', 'Proposal', 'Special Wishes', 'Love',
    'Friendship', 'Selfie on Screen', 'Other',
)
# Theatres need time to load a surprise onto the screen.
DEFAULT_LEAD_HOURS = 3

# --- Limits (vendor JSON is used as loop bounds — keep it sane) -------------

MAX_PLANS = 30
MAX_PACKAGES = 30
MAX_SHOWS = 60
MAX_TIMES_PER_SHOW = 12
MAX_SLOTS = 24
MAX_CLOSURES = 60
MAX_LIST_ITEMS = 40
MAX_PRICE = 1_000_000
MAX_PERSONS = 1000
MAX_SCREENS = 50


def option_lists():
    """Every list above, for GET /api/offerings/options."""
    return {
        'weekdays': list(WEEKDAYS),
        'fitnessServices': list(FITNESS_SERVICES),
        'fitnessAmenities': list(FITNESS_AMENITIES),
        'planKinds': [{'key': k, **v} for k, v in PLAN_KINDS.items()],
        'resortTypes': list(RESORT_TYPES),
        'resortFacilities': {k: list(v) for k, v in RESORT_FACILITIES.items()},
        'resortInclusions': list(RESORT_INCLUSIONS),
        'cinemaBrands': list(CINEMA_BRANDS),
        'theatreTypes': list(THEATRE_TYPES),
        'screenFormats': list(SCREEN_FORMATS),
        'surpriseOccasions': list(SURPRISE_OCCASIONS),
    }
