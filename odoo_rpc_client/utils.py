# -*- encoding: utf-8 -*-
# Copyright © 2014-2018 Dmytro Katyukha <dmytro.katyukha@gmail.com>

#######################################################################
# This Source Code Form is subject to the terms of the Mozilla Public #
# License, v. 2.0. If a copy of the MPL was not distributed with this #
# file, You can obtain one at http://mozilla.org/MPL/2.0/.            #
#######################################################################

import re
import functools

from packaging.version import parse as _parse_version, InvalidVersion

__all__ = ('ustr',
           'AttrDict',
           'DirMixIn',
           'UConverter',
           'wpartial',
           'parse_version',
           )

# Check if anyfield is installed
# and import function which converts SField instances to functions
try:
    from anyfield import toFn as normalizeSField
except ImportError:
    def normalizeSField(fn):
        return fn


def parse_version(version_string):
    """ Parse a version string into a comparable object.

        Replacement for the removed ``pkg_resources.parse_version`` that is
        tolerant of the non `PEP 440`_ version strings Odoo may report, such
        as ``saas~17.2`` or ``saas~16.3+e``.

        :param version_string: version to parse (e.g. ``'17.0'``)
        :return: parsed version, comparable with other parsed versions
        :rtype: packaging.version.Version

        .. _PEP 440: https://peps.python.org/pep-0440/
    """
    try:
        return _parse_version(version_string)
    except (InvalidVersion, TypeError):
        # Odoo online/SaaS builds report versions like 'saas~17.2' which are
        # not valid PEP 440. Strip the 'saas~' marker and retry.
        cleaned = re.sub(r'^saas[~-]?', '', str(version_string))
        try:
            return _parse_version(cleaned)
        except InvalidVersion:
            # Last resort: keep only the leading dotted-numeric part.
            match = re.match(r'\d+(?:\.\d+)*', cleaned)
            return _parse_version(match.group(0) if match else '0')


def wpartial(func, *args, **kwargs):
    """Wrapped partial, same as functools.partial decorator,
       but also calls functools.wrap on its result thus shwing correct
       function name and representation.
    """
    partial = functools.partial(func, *args, **kwargs)

    return functools.wraps(func)(partial)


def preprocess_args(*args, **kwargs):
    """ Skip all args, and kwargs that set to None

        Mostly for internal usage.

        Used to workaround xmlrpc None restrictions
    """
    kwargs = {key: val for key, val in kwargs.items() if val is not None}

    # TODO: review this! It may bring errors
    xargs = list(args[:])
    while xargs and xargs[-1] is None:
        xargs.pop()
    return xargs, kwargs


def stdcall(fn):
    """ Simple decorator for server methods, that supports standard call

        If method supports call like
        ``method(ids, <args>, context=context, <kwargs>)``,
        then it may be decorated by this decorator to appear in
        dir(record) and dir(recordlist) calls, thus making it available
        for autocompletition in ipython or other python shells
    """
    fn.__x_stdcall__ = True
    return fn


class UConverter(object):
    """ Simple converter to unicode

        Create instance with specified list of encodings to be used to
        try to convert value to unicode

        Example::

            ustr = UConverter(['utf-8', 'cp-1251'])
            my_unicode_str = ustr(b'hello - привет')
    """
    default_encodings = ['utf-8', 'ascii']

    def __init__(self, hint_encodings=None):
        if hint_encodings:
            self.encodings = hint_encodings
        else:
            self.encodings = self.default_encodings[:]

    def __call__(self, value):
        """ Convert value to unicode

        :param value: the value to convert
        :raise: UnicodeError if value cannot be coerced to unicode
        :return: unicode string representing the given value
        """
        # it is already a (unicode) string
        if isinstance(value, str):
            return value

        # it is not bytes: try a direct conversion to str
        if not isinstance(value, bytes):
            try:
                value = str(value)
            except Exception:
                # Cannot directly convert to str. So let's try to convert
                # to bytes, and then try diferent encoding to it
                try:
                    value = bytes(value)
                except Exception:
                    raise UnicodeError('unable to convert to unicode %r'
                                       '' % (value,))
            else:
                return value

        # value is bytes: decode using the configured encodings
        for ln in self.encodings:
            try:
                res = str(value, ln)
            except Exception:
                pass
            else:
                return res

        raise UnicodeError('unable to convert to unicode %r' % (value,))


# default converter instance
ustr = UConverter()


# DirMixIn is kept as an (empty) mix-in for backward compatibility: on
# Python 3 the base ``object`` already implements ``__dir__`` which can be
# accessed via ``super()`` by subclasses, so no extra logic is needed.
class DirMixIn:
    pass


class AttrDict(dict, DirMixIn):
    """ Simple class to make dictionary able to use attribute get operation
        to get elements it contains using syntax like:

        >>> d = AttrDict(arg1=1, arg2='hello')
        >>> print(d.arg1)
            1
        >>> print(d.arg2)
            hello
        >>> print(d['arg2'])
            hello
        >>> print(d['arg1'])
            1
    """
    def __getattr__(self, name):
        res = None
        try:
            res = super(AttrDict, self).__getitem__(name)
        except KeyError as e:
            raise AttributeError(str(e))
        return res

    def __dir__(self):
        res = super(AttrDict, self).__dir__() + list(self.keys())
        return list(set(res))
