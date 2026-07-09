# -*- coding: utf-8 -*-
# Copyright © 2014-2018 Dmytro Katyukha <dmytro.katyukha@gmail.com>

#######################################################################
# This Source Code Form is subject to the terms of the Mozilla Public #
# License, v. 2.0. If a copy of the MPL was not distributed with this #
# file, You can obtain one at http://mozilla.org/MPL/2.0/.            #
#######################################################################

""" Connector for the Odoo 17.0+ *External JSON-2 API*.

This connector talks to the ``POST /json/2/<model>/<method>`` endpoint
introduced by Odoo and authenticates with an **API key** (bearer token)
instead of a login/password pair.

Only the *transport* layer changes: every model call still funnels through
``execute_kw`` exactly like the XML-RPC / JSON-RPC connectors, so the whole
ORM layer on top of it (``Record``/``RecordList``, lazy relational access,
etc.) keeps working unchanged.

Usage -- exactly the same ``Client`` arguments as the other connectors, just
pass the **API key as** ``pwd`` and select the ``json-2`` protocol::

    from odoo_rpc_client import Client
    cl = Client(
        host='mycompany.odoo.com',
        dbname='mycompany',       # sent as the X-Odoo-Database header
        user='automation',        # only satisfies Client's login guard (ignored)
        pwd='<api-key>',          # <-- the API key (bearer token)
        protocol='json-2',
        port=443,
    )
    cl['res.partner'].search_records([('is_company', '=', True)])

The API key and database are taken from the ``pwd`` and ``dbname`` arguments
that the ORM already passes to ``execute_kw`` / ``login`` on every call, so no
special extra arguments are needed. They may still be overridden explicitly:

    - ``api_key``: Odoo API key (bearer token). Defaults to the Client ``pwd``.
    - ``database``: value for the ``X-Odoo-Database`` header. Defaults to the
      Client ``dbname``.
    - ``base_url``: full base url override (e.g. ``https://host/odoo``).
      When omitted it is built from host/port/ssl.
    - ``ssl``: whether to use https (default: ``True``).
    - ``ssl_verify``: verify TLS certificates (default: ``True``).
    - ``uid``: user id reported to the ORM. Informational only, as auth is
      done through the API key (default: ``1``).
    - ``server_version``: skip auto-detection and report this Odoo version
      (e.g. ``'17.0'``).
"""

import logging

import requests

from .connection import ConnectorBase, DEFAULT_TIMEOUT
from .. import exceptions

logger = logging.getLogger(__name__)


# Mapping of positional arguments to their JSON-2 body key, per model method.
# The JSON-2 endpoint expects method arguments as named keys in the request
# body, while the ORM passes some of them positionally (see
# ``odoo_rpc_client.orm.object.Object``). This table bridges the two.
POSITIONAL_PARAMS = {
    'read':         ['ids', 'fields'],
    'write':        ['ids', 'vals'],
    'create':       ['vals_list'],
    'unlink':       ['ids'],
    'search':       ['domain', 'offset', 'limit', 'order', 'count'],
    'search_read':  ['domain', 'fields', 'offset', 'limit', 'order'],
    'search_count': ['domain'],
    'name_search':  ['name', 'args', 'operator', 'limit'],
    'name_get':     ['ids'],
    'copy':         ['default'],
    'default_get':  ['fields_list'],
    'fields_get':   ['allfields', 'attributes'],
    'read_group':   ['domain', 'fields', 'groupby', 'offset', 'limit',
                     'orderby', 'lazy'],
}


class JSON2Error(exceptions.ConnectorError):
    """ Error raised for failed JSON-2 requests """
    def __init__(self, message, code=None, data=None):
        self.message = message
        self.code = code
        self.data = data
        super(JSON2Error, self).__init__(message)


class JSON2Requester(object):
    """ Low-level helper that performs the actual HTTP calls to the
        ``/json/2`` endpoint. Shared by all service proxies of a connection.
    """

    def __init__(self, base_url, api_key=None, database=None, ssl_verify=True,
                 timeout=DEFAULT_TIMEOUT):
        self._base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._database = database
        self._ssl_verify = ssl_verify
        self._timeout = timeout

    def set_credentials(self, database=None, api_key=None):
        """ Refresh the API key / database from the ``dbname`` and ``pwd``
            arguments the ORM passes on every ``execute_kw`` / ``login`` call.
            Only non-empty values overwrite the current ones.
        """
        if api_key:
            self._api_key = api_key
        if database:
            self._database = database

    @property
    def headers(self):
        if not self._api_key:
            raise JSON2Error(
                "No API key available for JSON-2 authentication. Pass it as "
                "the Client 'pwd' argument (or as the 'api_key' argument).")
        headers = {
            'Authorization': 'bearer %s' % self._api_key,
            'Content-Type': 'application/json',
            'User-Agent': 'odoo-rpc-client (json-2)',
        }
        if self._database:
            headers['X-Odoo-Database'] = self._database
        return headers

    @property
    def api_key(self):
        return self._api_key

    @staticmethod
    def _build_body(method, args, kwargs):
        """ Build the JSON request body from positional + keyword arguments.
        """
        # keyword arguments (drop None, they are just defaults)
        body = {key: val for key, val in (kwargs or {}).items()
                if val is not None}

        if args:
            names = POSITIONAL_PARAMS.get(method)
            if names:
                for name, value in zip(names, args):
                    body[name] = value
                extra = list(args[len(names):])
                if extra:
                    # More positional arguments than we know names for:
                    # pass them through as a best-effort ``args`` list.
                    body.setdefault('args', []).extend(extra)
            else:
                # Unknown (custom) method: pass positional args best-effort.
                body['args'] = list(args)
        return body

    def call(self, model, method, args=None, kwargs=None):
        """ Perform ``POST /json/2/<model>/<method>`` and return the parsed
            JSON result.

            :raises JSON2Error: on transport errors or non 2xx responses.
        """
        url = '%s/json/2/%s/%s' % (self._base_url, model, method)
        body = self._build_body(method, args or (), kwargs or {})

        try:
            resp = requests.post(url, json=body, headers=self.headers,
                                 verify=self._ssl_verify, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise JSON2Error("Cannot reach %s: %s" % (url, exc))

        if resp.status_code >= 400:
            detail = resp.text
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    detail = (payload.get('error')
                              or payload.get('message')
                              or payload)
            except ValueError:
                pass
            raise JSON2Error(
                "JSON-2 call %s/%s failed [HTTP %s]: %s"
                % (model, method, resp.status_code, detail),
                code=resp.status_code)

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise JSON2Error(
                "Cannot decode JSON-2 response for %s/%s: %s"
                % (model, method, resp.text[:500]))


class JSON2ObjectProxy(object):
    """ Emulates the 'object' service over JSON-2.

        Provides the single ``execute_kw`` entry point the ORM relies on.
    """
    def __init__(self, requester):
        self._requester = requester

    def execute_kw(self, dbname, uid, pwd, model, method, args, kwargs):
        # The API key comes in as ``pwd`` and the database as ``dbname``.
        self._requester.set_credentials(dbname, pwd)
        result = self._requester.call(model, method, args, kwargs)

        # ``create`` over classic RPC returns a scalar id; JSON-2 may return a
        # single-element list (recordset). Unwrap it so the ORM's single
        # ``create`` keeps returning a scalar id.
        if method == 'create' and isinstance(result, list) and len(result) == 1:
            return result[0]
        return result

    def execute(self, dbname, uid, pwd, model, method, *args):
        self._requester.set_credentials(dbname, pwd)
        return self._requester.call(model, method, list(args), {})

    def exec_workflow(self, *args, **kwargs):  # pragma: no cover
        raise JSON2Error("Workflow calls are not supported over JSON-2")


class JSON2CommonProxy(object):
    """ Emulates the 'common' service over JSON-2.

        There is no login round-trip with API-key auth, so ``login`` just
        reports the configured user id.
    """
    def __init__(self, requester, uid=1):
        self._requester = requester
        self._uid = uid

    def login(self, dbname, user, password):
        # ``password`` is the API key; capture it (and the database) so the
        # first authenticated request has them available.
        self._requester.set_credentials(dbname, password)
        return self._uid

    def authenticate(self, dbname, user, password, user_agent_env=None):
        self._requester.set_credentials(dbname, password)
        return self._uid

    def version(self):  # pragma: no cover
        return {}


class JSON2DbProxy(object):
    """ Emulates the 'db' service over JSON-2 (only version detection). """
    def __init__(self, requester, server_version=None):
        self._requester = requester
        self._server_version = server_version

    def server_version(self):
        if self._server_version:
            return self._server_version
        # Detect the server version from the 'base' module. This goes straight
        # through the requester (not the ORM) to avoid recursing into the
        # version-gated ORM code paths. It needs the API key, which is captured
        # from the first ``login`` / ``execute_kw`` call.
        if self._requester.api_key:
            try:
                res = self._requester.call(
                    'ir.module.module', 'search_read', (),
                    {'domain': [('name', '=', 'base')],
                     'fields': ['latest_version'],
                     'limit': 1})
                if res:
                    version = res[0].get('latest_version')
                    if version:
                        self._server_version = version
                        return version
            except Exception as exc:  # pragma: no cover
                logger.warning("Could not detect Odoo server version: %s", exc)
        # No key yet (or detection failed): report a modern default so that all
        # version-gated features stay enabled. Not cached, so a later call can
        # still auto-detect once the key is available.
        return '17.0'


class ConnectorJSON2(ConnectorBase):
    """ JSON-2 connector (Odoo 17.0+ ``/json/2`` API, API-key auth).

        See the module docstring for available extra arguments.
    """
    class Meta:
        name = 'json-2'
        use_ssl = True

    def __init__(self, *args, **kwargs):
        super(ConnectorJSON2, self).__init__(*args, **kwargs)
        self._requester = None

    def _get_base_url(self):
        base_url = self.extra_args.get('base_url')
        if base_url:
            return base_url
        use_ssl = self.extra_args.get('ssl', self.Meta.use_ssl)
        scheme = 'https' if use_ssl else 'http'
        netloc = self.host
        if self.port:
            netloc = '%s:%s' % (netloc, self.port)
        return '%s://%s' % (scheme, netloc)

    def get_requester(self):
        if self._requester is None:
            # api_key / database default to the Client 'pwd' / 'dbname', which
            # the ORM feeds in on every execute_kw / login call. They may also
            # be provided explicitly as connector arguments.
            self._requester = JSON2Requester(
                self._get_base_url(),
                api_key=self.extra_args.get('api_key'),
                database=self.extra_args.get('database'),
                ssl_verify=self.extra_args.get('ssl_verify', True),
                timeout=self.timeout)
        return self._requester

    def _get_service(self, name):
        requester = self.get_requester()
        if name == 'object':
            return JSON2ObjectProxy(requester)
        if name == 'common':
            return JSON2CommonProxy(requester,
                                    uid=self.extra_args.get('uid', 1))
        if name == 'db':
            return JSON2DbProxy(
                requester,
                server_version=self.extra_args.get('server_version'))
        raise JSON2Error("Service '%s' is not supported over JSON-2" % name)


class ConnectorJSON2S(ConnectorJSON2):
    """ Alias connector that makes the https default explicit (``json-2s``). """
    class Meta:
        name = 'json-2s'
        use_ssl = True
