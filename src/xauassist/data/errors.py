"""Errors raised by market-data providers."""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for market-data provider failures."""


class ConnectionFailedError(ProviderError):
    """The terminal/connection is unavailable or was lost."""


class SymbolNotFoundError(ProviderError):
    """The requested symbol is not available from the provider."""


class DataUnavailableError(ProviderError):
    """The connection is fine but no usable candle data was returned (e.g. history syncing)."""


class DataFormatError(ProviderError):
    """Candle data (e.g. a CSV file) could not be parsed."""
