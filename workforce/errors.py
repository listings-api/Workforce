class WorkforceError(Exception):
    """Base error for WorkForce Agent."""


class ConfigError(WorkforceError):
    """workforce.toml is missing, malformed, or has an invalid value."""


class StateLockedError(WorkforceError):
    """Another workforce process holds the state lock."""


class PreflightError(WorkforceError):
    """A startup safety check failed (API key set, CLI missing, not logged in, dirty repo)."""


class ModelUnavailable(WorkforceError):
    """A configured model is not available on the user's subscription."""


class GitError(WorkforceError):
    """A git operation failed."""
