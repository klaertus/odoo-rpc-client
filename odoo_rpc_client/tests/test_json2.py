# -*- coding: utf-8 -*-
# Copyright © 2014-2018 Dmytro Katyukha <dmytro.katyukha@gmail.com>

#######################################################################
# This Source Code Form is subject to the terms of the Mozilla Public #
# License, v. 2.0. If a copy of the MPL was not distributed with this #
# file, You can obtain one at http://mozilla.org/MPL/2.0/.            #
#######################################################################

""" Offline unit tests for the JSON-2 connector.

These tests mock the HTTP layer, so they run without a live Odoo server and
verify both the request wire-format (URL, headers, body) and that the ORM
layer -- including lazy relational (many2one) access -- keeps working on top
of the JSON-2 transport.
"""

import unittest
from unittest import mock

from odoo_rpc_client import Client
from odoo_rpc_client.connection import get_connector_names
import odoo_rpc_client.connection.json2 as json2


PARTNER_FIELDS = {
    'id': {'type': 'integer', 'string': 'ID'},
    'name': {'type': 'char', 'string': 'Name'},
    'parent_id': {'type': 'many2one', 'relation': 'res.partner',
                  'string': 'Parent'},
}


class FakeResp(object):
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = repr(data)
        self.content = b'x'

    def json(self):
        return self._data


class TestJSON2Connector(unittest.TestCase):

    def setUp(self):
        self.calls = []

        def router(url, json=None, headers=None, verify=None, timeout=None):
            body = json or {}
            self.calls.append((url, body, headers))
            model, method = url.split('/json/2/', 1)[1].split('/')

            if model == 'ir.module.module' and method == 'search_read':
                return FakeResp([{'id': 1, 'latest_version': '17.0.1.3'}])
            if model == 'res.partner' and method == 'fields_get':
                return FakeResp(PARTNER_FIELDS)
            if model == 'res.partner' and method == 'search':
                return FakeResp([10])
            if model == 'res.partner' and method == 'search_read':
                return FakeResp([
                    {'id': 10, 'name': 'ACME', 'parent_id': [5, 'ACME Group']},
                ])
            if model == 'res.partner' and method == 'read':
                ids = body.get('ids')
                ids = ids if isinstance(ids, list) else [ids]
                out = []
                for i in ids:
                    if i == 5:
                        out.append({'id': 5, 'name': 'ACME Group',
                                    'parent_id': False})
                    else:
                        out.append({'id': i, 'name': 'ACME',
                                    'parent_id': [5, 'ACME Group']})
                return FakeResp(out)
            if model == 'res.partner' and method == 'create':
                return FakeResp([42])   # recordset serialized as a list
            if model == 'res.partner' and method == 'write':
                return FakeResp(True)
            raise AssertionError("unexpected call %s/%s" % (model, method))

        patcher = mock.patch.object(json2.requests, 'post', side_effect=router)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Same Client arguments as any other connector: the API key is passed
        # as `pwd` and the database as `dbname` (no special extra arguments).
        self.client = Client(
            host='example.odoo.com', dbname='exampledb',
            user='apikey', pwd='THEKEY', protocol='json-2', port=443)

    def _body(self, endpoint):
        for url, body, _ in self.calls:
            if url.endswith(endpoint):
                return body
        raise AssertionError("no call to %s" % endpoint)

    def test_connector_registered(self):
        self.assertIn('json-2', get_connector_names())

    def test_server_version_autodetect(self):
        self.assertGreaterEqual(str(self.client.server_version), '17')

    def test_search_read_wire_format(self):
        # search_read maps its keyword arguments straight into the body
        self.client['res.partner'].search_read(
            domain=[('is_company', '=', True)], fields=['name', 'parent_id'])
        body = self._body('res.partner/search_read')
        self.assertIn('domain', body)
        self.assertIn('fields', body)

    def test_auth_and_database_headers(self):
        self.client['res.partner'].search_records([], read_fields=['name'])
        # every request carries the bearer token and database header
        _, _, headers = self.calls[0]
        self.assertEqual(headers['Authorization'], 'bearer THEKEY')
        self.assertEqual(headers['X-Odoo-Database'], 'exampledb')

    def test_nested_many2one_access(self):
        # The whole point: relational (lazy) access must still work.
        partners = self.client['res.partner'].search_records(
            [], read_fields=['name', 'parent_id'])
        partner = partners[0]
        self.assertEqual(partner.id, 10)
        self.assertEqual(partner.parent_id.id, 5)
        self.assertEqual(partner.parent_id.name, 'ACME Group')

    def test_create_returns_scalar_id(self):
        new = self.client['res.partner'].create({'name': 'New Co'})
        self.assertEqual(new, 42)
        self.assertEqual(self._body('res.partner/create'),
                         {'vals_list': {'name': 'New Co'}})

    def test_write_wire_format(self):
        self.assertIs(
            self.client['res.partner'].write(10, {'name': 'X'}), True)
        self.assertEqual(self._body('res.partner/write'),
                         {'ids': 10, 'vals': {'name': 'X'}})


if __name__ == '__main__':
    unittest.main()
