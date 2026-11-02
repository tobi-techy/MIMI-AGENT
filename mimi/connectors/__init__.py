"""Mimi's connector interface (RAI-277): read a shop tool, propose writes, apply, undo."""

from mimi.connectors.base import Connector, ConnectorError
from mimi.connectors.types import Proposal, ReadResult, Row, WriteRecord

__all__ = ["Connector", "ConnectorError", "Proposal", "ReadResult", "Row", "WriteRecord"]
