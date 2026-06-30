"""OTLP logging handler tailored for oslo.log.

The upstream ``LoggingHandler`` copies every non-reserved record attribute onto
the exported log record verbatim, which would emit oslo's raw ``RequestContext``
object and leave the useful identity fields unmapped. :class:`OsloLogHandler`
drops that object and projects a curated, normalised subset of the request
context (request id, user/project ids, ...) onto the record instead.

No formatter is set, so the exported body is the raw log message; the
structured fields travel as attributes.
"""

import logging
from typing import Any

from opentelemetry.instrumentation.logging.handler import LoggingHandler

try:
    from oslo_context import context as oslo_context
except ImportError:  # pragma: no cover - oslo.context ships with oslo.log
    oslo_context = None

# oslo.context logging value -> exported attribute key. Only non-sensitive
# identity fields are mapped; tokens are never exported.
_OSLO_CONTEXT_ATTRIBUTE_MAP = {
    "request_id": "openstack.request_id",
    "global_request_id": "openstack.global_request_id",
    "user": "openstack.user_id",
    "user_name": "openstack.user_name",
    "project": "openstack.project_id",
    "project_name": "openstack.project_name",
    "domain": "openstack.domain_id",
    "user_domain": "openstack.user_domain_id",
    "project_domain": "openstack.project_domain_id",
    "roles": "openstack.roles",
    "resource_uuid": "openstack.resource_uuid",
}

_SCALAR_TYPES = (bool, str, int, float)


def _coerce(value: Any) -> Any:
    """Coerce an oslo context value to a type OTel accepts as an attribute."""
    if isinstance(value, _SCALAR_TYPES):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, _SCALAR_TYPES) for item in value
    ):
        return list(value)
    return str(value)


def _logging_values(context: Any) -> dict:
    """Return oslo.context logging values as a plain dict."""
    if isinstance(context, dict):
        return context
    try:
        return context.get_logging_values()
    except Exception:  # pylint: disable=broad-except
        return {}


class OsloLogHandler(LoggingHandler):
    """A ``LoggingHandler`` that maps oslo.log request context onto attrs."""

    def __init__(self, *args, map_oslo_context: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self._map_oslo_context = map_oslo_context

    def _get_attributes(self, record: logging.LogRecord) -> dict:
        attributes = super()._get_attributes(record)
        # The base handler copies the raw RequestContext object and oslo's
        # bookkeeping key verbatim; drop them for the mapped fields instead.
        attributes.pop("context", None)
        attributes.pop("extra_keys", None)
        if self._map_oslo_context:
            attributes.update(self._oslo_context_attributes(record))
        return attributes

    def _oslo_context_attributes(self, record: logging.LogRecord) -> dict:
        context = getattr(record, "context", None)
        if context is None and oslo_context is not None:
            context = oslo_context.get_current()
        if context is None:
            return {}
        values = _logging_values(context)
        return {
            attribute_key: _coerce(values[source_key])
            for source_key, attribute_key in _OSLO_CONTEXT_ATTRIBUTE_MAP.items()
            if values.get(source_key) not in (None, "", [])
        }
