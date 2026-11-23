"""Exceptions."""


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate there is invalid auth."""


class MfaRequired(InvalidAuth):
    """The account needs a one-time code; the server has just sent one.

    Subclasses InvalidAuth so unattended polling falls through to reauth.
    """

    def __init__(self, description: str) -> None:
        """Keep the challenge id that follows "BUS-MFAR:"."""
        super().__init__(description)
        self.mfa_token = description.partition(":")[2]
