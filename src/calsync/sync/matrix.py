"""Source -> [(target, mode)] fan-out matrix.

Codifies the user's intended topology:

| Source \\ Target | personal | avela | beachmedia | novact |
|------------------|----------|-------|------------|--------|
| personal         |    -     | busy  | busy       | busy   |
| avela            | full     |   -   | busy       | busy   |
| beachmedia       | full     | busy  |   -        | busy   |
| novact           | full     | busy  | busy       |   -    |

`personal` aggregates full event details from work; work cross-mirror
as `busy`; personal also broadcasts `busy` to work so colleagues see
availability.

Future: load from a YAML config so non-default deployments don't need
code changes.
"""

from typing import Literal

Mode = Literal['full', 'busy']

# Edges are (source_label, target_label, mode).
DEFAULT_MATRIX: list[tuple[str, str, Mode]] = [
    # personal -> busy on every work account
    ('personal', 'avela', 'busy'),
    ('personal', 'beachmedia', 'busy'),
    ('personal', 'novact', 'busy'),
    # avela
    ('avela', 'personal', 'full'),
    ('avela', 'beachmedia', 'busy'),
    ('avela', 'novact', 'busy'),
    # beachmedia
    ('beachmedia', 'personal', 'full'),
    ('beachmedia', 'avela', 'busy'),
    ('beachmedia', 'novact', 'busy'),
    # novact
    ('novact', 'personal', 'full'),
    ('novact', 'avela', 'busy'),
    ('novact', 'beachmedia', 'busy'),
]

PERSONAL_ACCOUNT_LABEL = 'personal'


def targets_for_source(source_label: str, matrix: list[tuple[str, str, Mode]] | None = None) -> list[tuple[str, Mode]]:
    """Return the list of (target_label, mode) pairs for the given source."""
    rows = matrix if matrix is not None else DEFAULT_MATRIX
    return [(tgt, mode) for src, tgt, mode in rows if src == source_label]
