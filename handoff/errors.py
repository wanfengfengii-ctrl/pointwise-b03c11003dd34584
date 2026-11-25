"""Domain errors shared by the handoff service and the CLI."""

from __future__ import annotations


class HandoffError(Exception):
    """A rejected operation.

    ``code`` is a stable machine-readable identifier.  ``details`` is merged
    into the JSON error object.
    """

    def __init__(self, code: str, message: str = "", **details):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.details = details

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        payload.update(self.details)
        return payload
