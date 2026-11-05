class InboxError(Exception):
    def __init__(self, message, status=400, *, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
