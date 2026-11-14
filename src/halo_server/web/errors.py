"""Content-free errors at the browser trust boundary."""


class WebError(RuntimeError):
    def __init__(self, code, message, *, status=400, possibly_sent=False):
        self.code, self.status, self.possibly_sent = code, status, possibly_sent
        super().__init__(message)


def fields(value, required=(), optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional):
        raise WebError("bad_request", "Request fields are invalid")
    if set(required) - set(value):
        raise WebError("bad_request", "Required request fields are missing")
    return value


def text(value, *, maximum=256, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise WebError("bad_request", "A text value is invalid")
    if any(ord(character) < 32 for character in value):
        raise WebError("bad_request", "A text value contains invalid characters")
    return value


def integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value < 2**53:
        raise WebError("bad_request", "An integer value is invalid")
    return value


def boolean(value):
    if type(value) is not bool:
        raise WebError("bad_request", "A boolean value is invalid")
    return value
