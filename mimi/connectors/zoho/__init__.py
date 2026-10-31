"""Zoho connectors: Books reads and writes, CRM reads and notes (RAI-277)."""

from mimi.connectors.zoho.books import ZohoBooksConnector
from mimi.connectors.zoho.crm import ZohoCrmConnector

__all__ = ["ZohoBooksConnector", "ZohoCrmConnector"]
