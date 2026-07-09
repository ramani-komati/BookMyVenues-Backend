from rest_framework.pagination import PageNumberPagination


class VenuePagination(PageNumberPagination):
    """
    Standard page-based pagination for list endpoints.
    Response shape: {count, next, previous, results: [...]}
    Client controls it with ?page=2&page_size=50 (capped at 100).
    """

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
