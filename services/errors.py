class ToolError(Exception):
    """Raised for any user-input problem that should surface as a friendly,
    actionable message rather than a stack trace or generic 500."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message
