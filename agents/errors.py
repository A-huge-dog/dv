"""Typed failures shared by the Agent protocol and its persistence."""


class AgentLoopError(RuntimeError):
    """Typed failure raised at the Agent protocol boundary."""

    def __init__(self, code: str, message: str):
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message
