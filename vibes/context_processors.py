from .models import Source


def sections(request):
    """Nav needs the two sections regardless of which page rendered."""
    return {"sections": Source.Kind.choices}
