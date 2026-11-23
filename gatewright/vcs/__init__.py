# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Git hosting integration (v0.5): detect pushes and pull requests, report
commit statuses per gate, comment on pull requests.

Every git host is an adapter implementing `VCSProvider` (base.py):

    list_events(state)     what changed since last time (pushes + open PRs), via polling
    parse_webhook(...)     the same events, pushed to us by the host
    set_status(...)        ✅ / ❌ / 🟡 next to the commit, one "context" per gate
    comment(...)           summary on the pull/merge request
    fetch_refspec(event)   where to fetch the exact commit from

Adapters: github.py (GitHub), gitea.py (Gitea and Forgejo, same API), gitlab.py.
To add a host, implement VCSProvider and register it in PROVIDERS below.
"""

from .base import VCSConfig, VCSError, VCSEvent, VCSProvider  # noqa: F401
from .gitea import GiteaProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

PROVIDERS: dict[str, type[VCSProvider]] = {
    "github": GitHubProvider,
    "gitea": GiteaProvider,
    "forgejo": GiteaProvider,
    "gitlab": GitLabProvider,
}


def make_provider(cfg: VCSConfig, token: str | None) -> VCSProvider:
    """Create the adapter for `cfg.provider` with an API token (None = anonymous)."""
    try:
        cls = PROVIDERS[cfg.provider]
    except KeyError:
        raise VCSError(f"Unknown git host provider `{cfg.provider}`. Use one of: {', '.join(PROVIDERS)}.") from None
    return cls(cfg, token)
