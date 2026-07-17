"""
Venue serializers.

The old wizard serializers were removed when the API moved to the
frontend's draft-bucket contract (drafts store raw JSON, so no DRF
serializers are needed — see draft_validation.py for format checks).

Phase 4 (publish + public browsing) adds the listing serializers here.
"""
