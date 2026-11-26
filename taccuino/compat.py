"""Compatibilità: le differenze tra Linux e Windows stanno in `taccuino/platforms/`; questo modulo le riesporta."""
from .platforms import IS_WIN, NO_WINDOW, command, try_lock  # noqa: F401
