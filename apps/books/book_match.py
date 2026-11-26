import re
from heapq import nlargest

from django.db.models import Q


FILLER = frozenset(
    "a an and are about book books by can could find finish for from get give i i'd in is "
    "it like looking me my of on please read reading recommend show some something that "
    "the this to want we weekend what with would you".split()
)
RELATED_TERMS = {
    "mystery": ("mystery", "mysteries", "detective", "whodunit"),
    "romance": ("romance", "romantic", "love story"),
    "scifi": ("scifi", "sci-fi", "science fiction"),
    "short": ("short", "novella", "short story", "short stories"),
    "funny": ("funny", "humor", "humour", "comedy"),
    "history": ("history", "historical"),
}


def words(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def recommendations(books, prompt):
    normalized = words(prompt).replace("science fiction", "scifi").replace("sci fi", "scifi")
    terms = list(
        dict.fromkeys(
            term for term in normalized.split() if len(term) > 1 and term not in FILLER
        )
    )[:12]
    if not terms:
        return []

    groups = [RELATED_TERMS.get(term, (term,)) for term in terms]
    fields = {"category": 5, "name": 4, "author": 4, "language": 4, "description": 2}
    candidates = Q()
    for alias in dict.fromkeys(alias for group in groups for alias in group):
        for field in fields:
            candidates |= Q(**{f"{field}__icontains": alias})
    indexed_groups = [[(alias, f" {words(alias)} ") for alias in group] for group in groups]

    def ranked():
        for book in books.filter(candidates).order_by("name", "pk").iterator():
            indexed = [
                (f" {words(getattr(book, field))} ", weight)
                for field, weight in fields.items()
            ]
            matches = []
            score = 0
            for group in indexed_groups:
                for text, weight in indexed:
                    match = next((alias for alias, phrase in group if phrase in text), None)
                    if match:
                        matches.append(match)
                        score += weight
                        break
            if matches:
                yield score, book, list(dict.fromkeys(matches))

    best = nlargest(3, ranked(), key=lambda item: (item[0], item[1].quantity > 0))
    return [{"book": book, "terms": matches} for _, book, matches in best]
