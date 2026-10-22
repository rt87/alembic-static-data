"""Errors raised by alembic-static-data."""

from __future__ import annotations


class StaticDataError(Exception):
    """Base class for all static data loading errors."""


class CsvFormatError(StaticDataError):
    """The CSV file is unreadable, empty, or its header/rows are inconsistent."""


class NaturalKeyError(StaticDataError):
    """The natural keys are missing from the CSV or the table, or are not unique in the CSV."""


class ColumnMismatchError(StaticDataError):
    """The CSV header names columns that do not exist in the target table."""


class ValueCoercionError(StaticDataError):
    """A CSV cell could not be converted to the type of its target column."""


class EnvironmentNotSetError(StaticDataError):
    """An environment column was configured, but no environment was given to select rows with."""


class OfflineModeError(StaticDataError):
    """Static data cannot be loaded in offline (--sql) mode."""
