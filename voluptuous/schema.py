"""Schema validation for Python data structures.

Given eg. a nested data structure like this:

    {
        'exclude': ['Users', 'Uptime'],
        'include': [],
        'set': {
            'snmp_community': 'public',
            'snmp_timeout': 15,
            'snmp_version': '2c',
        },
        'targets': {
            'localhost': {
                'exclude': ['Uptime'],
                'features': {
                    'Uptime': {
                        'retries': 3,
                    },
                    'Users': {
                        'snmp_community': 'monkey',
                        'snmp_port': 15,
                    },
                },
                'include': ['Users'],
                'set': {
                    'snmp_community': 'monkeys',
                },
            },
        },
    }

A schema like this:

    >>> settings = {
    ...   'snmp_community': str,
    ...   'retries': int,
    ...   'snmp_version': All(Coerce(str), Any('3', '2c', '1')),
    ... }
    >>> features = ['Ping', 'Uptime', 'Http']
    >>> schema = Schema({
    ...    'exclude': features,
    ...    'include': features,
    ...    'set': settings,
    ...    'targets': {
    ...      'exclude': features,
    ...      'include': features,
    ...      'features': {
    ...        str: settings,
    ...      },
    ...    },
    ... })

Validate like so:

    >>> schema({
    ...   'set': {
    ...     'snmp_community': 'public',
    ...     'snmp_version': '2c',
    ...   },
    ...   'targets': {
    ...     'exclude': ['Ping'],
    ...     'features': {
    ...       'Uptime': {'retries': 3},
    ...       'Users': {'snmp_community': 'monkey'},
    ...     },
    ...   },
    ... }) == {
    ...   'set': {'snmp_version': '2c', 'snmp_community': 'public'},
    ...   'targets': {
    ...     'exclude': ['Ping'],
    ...     'features': {'Uptime': {'retries': 3},
    ...                  'Users': {'snmp_community': 'monkey'}}}}
    True
"""
import collections
import inspect
import re
import sys
from contextlib import contextmanager

import error
import markers

if sys.version_info >= (3,):
    long = int
    unicode = str
    basestring = str
    ifilter = filter
    iteritems = lambda d: d.items()
else:
    from itertools import ifilter

    iteritems = lambda d: d.iteritems()


# options for extra keys
PREVENT_EXTRA = 0  # any extra key not in schema will raise an error
ALLOW_EXTRA = 1  # extra keys not in schema will be included in output
REMOVE_EXTRA = 2  # extra keys not in schema will be excluded from output


@contextmanager
def raises(exc, msg=None, regex=None):
    try:
        yield
    except exc as e:
        if msg is not None:
            assert str(e) == msg, '%r != %r' % (str(e), msg)
        if regex is not None:
            assert re.search(regex, str(e)), '%r does not match %r' % (str(e), regex)


class Object(dict):
    """Indicate that we should work with attributes, not keys."""

    def __init__(self, schema, cls=markers.UNDEFINED):
        self.cls = cls
        super(Object, self).__init__(schema)


class VirtualPathComponent(str):
    def __str__(self):
        return '<' + self + '>'

    def __repr__(self):
        return self.__str__()


class Schema(object):
    """A validation schema.

    The schema is a Python tree-like structure where nodes are pattern
    matched against corresponding trees of values.

    Nodes can be values, in which case a direct comparison is used, types,
    in which case an isinstance() check is performed, or callables, which will
    validate and optionally convert the value.
    """

    _extra_to_name = {
        REMOVE_EXTRA: 'REMOVE_EXTRA',
        ALLOW_EXTRA: 'ALLOW_EXTRA',
        PREVENT_EXTRA: 'PREVENT_EXTRA',
    }

    def __init__(self, schema, required=False, extra=PREVENT_EXTRA):
        """Create a new Schema.

        :param schema: Validation schema. See :module:`voluptuous` for details.
        :param required: Keys defined in the schema must be in the data.
        :param extra: Specify how extra keys in the data are treated:
            - :const:`~voluptuous.PREVENT_EXTRA`: to disallow any undefined
              extra keys (raise ``Invalid``).
            - :const:`~voluptuous.ALLOW_EXTRA`: to include undefined extra
              keys in the output.
            - :const:`~voluptuous.REMOVE_EXTRA`: to exclude undefined extra keys
              from the output.
            - Any value other than the above defaults to
              :const:`~voluptuous.PREVENT_EXTRA`
        """
        self.schema = schema
        self.required = required
        self.extra = int(extra)  # ensure the value is an integer
        self._compiled = self._compile(schema)

    def __repr__(self):
        return "<Schema(%s, extra=%s, required=%s) object at 0x%x>" % (
            self.schema, self._extra_to_name.get(self.extra, '??'),
            self.required, id(self))

    def __call__(self, data):
        """Validate data against this schema."""
        try:
            return self._compiled([], data)
        except error.MultipleInvalid:
            raise
        except error.Invalid as e:
            raise error.MultipleInvalid([e])
            # return self.validate([], self.schema, data)

    def _compile(self, schema):
        if schema is markers.Extra:
            return lambda _, v: v
        if isinstance(schema, Object):
            return self._compile_object(schema)
        if isinstance(schema, collections.Mapping):
            return self._compile_dict(schema)
        elif isinstance(schema, list):
            return self._compile_list(schema)
        elif isinstance(schema, tuple):
            return self._compile_tuple(schema)
        type_ = type(schema)
        if type_ is type:
            type_ = schema
        if type_ in (bool, int, long, str, unicode, float, complex, object,
                     list, dict, type(None)) or callable(schema):
            return _compile_scalar(schema)
        raise error.SchemaError('unsupported schema data type %r' %
                                type(schema).__name__)

    def _compile_mapping(self, schema, invalid_msg=None):
        """Create validator for given mapping."""
        invalid_msg = invalid_msg or 'mapping value'

        # Keys that may be required
        all_required_keys = set(key for key in schema
                                if key is not markers.Extra
                                and ((self.required and not isinstance(key, (markers.Optional, markers.Remove)))
                                     or isinstance(key, markers.Required)))

        # Keys that may have defaults
        all_default_keys = set(key for key in schema
                               if isinstance(key, markers.Required)
                               or isinstance(key, markers.Optional))

        _compiled_schema = {}
        for skey, svalue in iteritems(schema):
            new_key = self._compile(skey)
            new_value = self._compile(svalue)
            _compiled_schema[skey] = (new_key, new_value)

        candidates = list(_iterate_mapping_candidates(_compiled_schema))

        def validate_mapping(path, iterable, out):
            required_keys = all_required_keys.copy()
            # keeps track of all default keys that haven't been filled
            default_keys = all_default_keys.copy()
            error = None
            errors = []
            for key, value in iterable:
                key_path = path + [key]
                remove_key = False

                # compare each given key/value against all compiled key/values
                # schema key, (compiled key, compiled value)
                for skey, (ckey, cvalue) in candidates:
                    try:
                        new_key = ckey(key_path, key)
                    except error.Invalid as e:
                        if len(e.path) > len(key_path):
                            raise
                        if not error or len(e.path) > len(error.path):
                            error = e
                        continue
                    # Backtracking is not performed once a key is selected, so if
                    # the value is invalid we immediately throw an exception.
                    exception_errors = []
                    # check if the key is marked for removal
                    is_remove = new_key is markers.Remove
                    try:
                        cval = cvalue(key_path, value)
                        # include if it's not marked for removal
                        if not is_remove:
                            out[new_key] = cval
                        else:
                            remove_key = True
                            continue
                    except error.MultipleInvalid as e:
                        exception_errors.extend(e.errors)
                    except error.Invalid as e:
                        exception_errors.append(e)

                    if exception_errors:
                        if is_remove or remove_key:
                            continue
                        for err in exception_errors:
                            if len(err.path) <= len(key_path):
                                err.error_type = invalid_msg
                            errors.append(err)
                        # If there is a validation error for a required
                        # key, this means that the key was provided.
                        # Discard the required key so it does not
                        # create an additional, noisy exception.
                        required_keys.discard(skey)
                        break

                    # Key and value okay, mark any Required() fields as found.
                    required_keys.discard(skey)

                    # No need for a default if it was filled
                    default_keys.discard(skey)

                    break
                else:
                    if remove_key:
                        # remove key
                        continue
                    elif self.extra == ALLOW_EXTRA:
                        out[key] = value
                    elif self.extra != REMOVE_EXTRA:
                        errors.append(error.Invalid('extra keys not allowed', key_path))
                        # else REMOVE_EXTRA: ignore the key so it's removed from output

            # set defaults for any that can have defaults
            for key in default_keys:
                if not isinstance(key.default, Undefined):  # if the user provides a default with the node
                    out[key.schema] = key.default()
                    if key in required_keys:
                        required_keys.discard(key)

            # for any required keys left that weren't found and don't have defaults:
            for key in required_keys:
                msg = key.msg if hasattr(key, 'msg') and key.msg else 'required key not provided'
                errors.append(error.RequiredFieldInvalid(msg, path + [key]))
            if errors:
                raise error.MultipleInvalid(errors)

            return out

        return validate_mapping

    def _compile_object(self, schema):
        """Validate an object.

        Has the same behavior as dictionary validator but work with object
        attributes.

        For example:

            >>> class Structure(object):
            ...     def __init__(self, one=None, three=None):
            ...         self.one = one
            ...         self.three = three
            ...
            >>> validate = Schema(Object({'one': 'two', 'three': 'four'}, cls=Structure))
            >>> with raises(MultipleInvalid, "not a valid value for object value @ data['one']"):
            ...   validate(Structure(one='three'))

        """
        base_validate = self._compile_mapping(
            schema, invalid_msg='object value')

        def validate_object(path, data):
            if (schema.cls is not UNDEFINED
                and not isinstance(data, schema.cls)):
                raise error.ObjectInvalid('expected a {0!r}'.format(schema.cls), path)
            iterable = _iterate_object(data)
            iterable = ifilter(lambda item: item[1] is not None, iterable)
            out = base_validate(path, iterable, {})
            return type(data)(**out)

        return validate_object

    def _compile_dict(self, schema):
        """Validate a dictionary.

        A dictionary schema can contain a set of values, or at most one
        validator function/type.

        A dictionary schema will only validate a dictionary:

            >>> validate = Schema({})
            >>> with raises(MultipleInvalid, 'expected a dictionary'):
            ...   validate([])

        An invalid dictionary value:

            >>> validate = Schema({'one': 'two', 'three': 'four'})
            >>> with raises(MultipleInvalid, "not a valid value for dictionary value @ data['one']"):
            ...   validate({'one': 'three'})

        An invalid key:

            >>> with raises(MultipleInvalid, "extra keys not allowed @ data['two']"):
            ...   validate({'two': 'three'})


        Validation function, in this case the "int" type:

            >>> validate = Schema({'one': 'two', 'three': 'four', int: str})

        Valid integer input:

            >>> validate({10: 'twenty'})
            {10: 'twenty'}

        By default, a "type" in the schema (in this case "int") will be used
        purely to validate that the corresponding value is of that type. It
        will not Coerce the value:

            >>> with raises(MultipleInvalid, "extra keys not allowed @ data['10']"):
            ...   validate({'10': 'twenty'})

        Wrap them in the Coerce() function to achieve this:

            >>> validate = Schema({'one': 'two', 'three': 'four',
            ...                    Coerce(int): str})
            >>> validate({'10': 'twenty'})
            {10: 'twenty'}

        Custom message for required key

            >>> validate = Schema({Required('one', 'required'): 'two'})
            >>> with raises(MultipleInvalid, "required @ data['one']"):
            ...   validate({})

        (This is to avoid unexpected surprises.)

        Multiple errors for nested field in a dict:

        >>> validate = Schema({
        ...     'adict': {
        ...         'strfield': str,
        ...         'intfield': int
        ...     }
        ... })
        >>> try:
        ...     validate({
        ...         'adict': {
        ...             'strfield': 123,
        ...             'intfield': 'one'
        ...         }
        ...     })
        ... except MultipleInvalid as e:
        ...     print(sorted(str(i) for i in e.errors)) # doctest: +NORMALIZE_WHITESPACE
        ["expected int for dictionary value @ data['adict']['intfield']",
         "expected str for dictionary value @ data['adict']['strfield']"]

        """
        base_validate = self._compile_mapping(
            schema, invalid_msg='dictionary value')

        groups_of_exclusion = {}
        groups_of_inclusion = {}
        for node in schema:
            if isinstance(node, markers.Exclusive):
                g = groups_of_exclusion.setdefault(node.group_of_exclusion, [])
                g.append(node)
            elif isinstance(node, markers.Inclusive):
                g = groups_of_inclusion.setdefault(node.group_of_inclusion, [])
                g.append(node)

        def validate_dict(path, data):
            if not isinstance(data, dict):
                raise error.DictInvalid('expected a dictionary', path)

            errors = []
            for label, group in groups_of_exclusion.items():
                exists = False
                for exclusive in group:
                    if exclusive.schema in data:
                        if exists:
                            msg = exclusive.msg if hasattr(exclusive, 'msg') and exclusive.msg else \
                                "two or more values in the same group of exclusion '%s'" % label
                            next_path = path + [VirtualPathComponent(label)]
                            errors.append(error.ExclusiveInvalid(msg, next_path))
                            break
                        exists = True

            if errors:
                raise error.MultipleInvalid(errors)

            for label, group in groups_of_inclusion.items():
                included = [node.schema in data for node in group]
                if any(included) and not all(included):
                    msg = "some but not all values in the same group of inclusion '%s'" % label
                    for g in group:
                        if hasattr(g, 'msg') and g.msg:
                            msg = g.msg
                            break
                    next_path = path + [VirtualPathComponent(label)]
                    errors.append(error.InclusiveInvalid(msg, next_path))
                    break

            if errors:
                raise error.MultipleInvalid(errors)

            out = {}
            return base_validate(path, iteritems(data), out)

        return validate_dict

    def _compile_sequence(self, schema, seq_type):
        """Validate a sequence type.

        This is a sequence of valid values or validators tried in order.

        >>> validator = Schema(['one', 'two', int])
        >>> validator(['one'])
        ['one']
        >>> with raises(MultipleInvalid, 'expected int @ data[0]'):
        ...   validator([3.5])
        >>> validator([1])
        [1]
        """
        _compiled = [self._compile(s) for s in schema]
        seq_type_name = seq_type.__name__

        def validate_sequence(path, data):
            if not isinstance(data, seq_type):
                raise error.SequenceTypeInvalid('expected a %s' % seq_type_name, path)

            # Empty seq schema, allow any data.
            if not schema:
                return data

            out = []
            invalid = None
            errors = []
            index_path = UNDEFINED
            for i, value in enumerate(data):
                index_path = path + [i]
                invalid = None
                for validate in _compiled:
                    try:
                        cval = validate(index_path, value)
                        if cval is not markers.Remove:  # do not include Remove values
                            out.append(cval)
                        break
                    except error.Invalid as e:
                        if len(e.path) > len(index_path):
                            raise
                        invalid = e
                else:
                    errors.append(invalid)
            if errors:
                raise error.MultipleInvalid(errors)
            return type(data)(out)

        return validate_sequence

    def _compile_tuple(self, schema):
        """Validate a tuple.

        A tuple is a sequence of valid values or validators tried in order.

        >>> validator = Schema(('one', 'two', int))
        >>> validator(('one',))
        ('one',)
        >>> with raises(MultipleInvalid, 'expected int @ data[0]'):
        ...   validator((3.5,))
        >>> validator((1,))
        (1,)
        """
        return self._compile_sequence(schema, tuple)

    def _compile_list(self, schema):
        """Validate a list.

        A list is a sequence of valid values or validators tried in order.

        >>> validator = Schema(['one', 'two', int])
        >>> validator(['one'])
        ['one']
        >>> with raises(MultipleInvalid, 'expected int @ data[0]'):
        ...   validator([3.5])
        >>> validator([1])
        [1]
        """
        return self._compile_sequence(schema, list)

    def extend(self, schema, required=None, extra=None):
        """Create a new `Schema` by merging this and the provided `schema`.

        Neither this `Schema` nor the provided `schema` are modified. The
        resulting `Schema` inherits the `required` and `extra` parameters of
        this, unless overridden.

        Both schemas must be dictionary-based.

        :param schema: dictionary to extend this `Schema` with
        :param required: if set, overrides `required` of this `Schema`
        :param extra: if set, overrides `extra` of this `Schema`
        """

        assert type(self.schema) == dict and type(schema) == dict, 'Both schemas must be dictionary-based'

        result = self.schema.copy()
        result.update(schema)

        result_required = (required if required is not None else self.required)
        result_extra = (extra if extra is not None else self.extra)
        return Schema(result, required=result_required, extra=result_extra)


def _compile_scalar(schema):
    """A scalar value.

    The schema can either be a value or a type.

    >>> _compile_scalar(int)([], 1)
    1
    >>> with raises(Invalid, 'expected float'):
    ...   _compile_scalar(float)([], '1')

    Callables have
    >>> _compile_scalar(lambda v: float(v))([], '1')
    1.0

    As a convenience, ValueError's are trapped:

    >>> with raises(Invalid, 'not a valid value'):
    ...   _compile_scalar(lambda v: float(v))([], 'a')
    """
    if isinstance(schema, type):
        def validate_instance(path, data):
            if isinstance(data, schema):
                return data
            else:
                msg = 'expected %s' % schema.__name__
                raise error.TypeInvalid(msg, path)

        return validate_instance

    if callable(schema):
        def validate_callable(path, data):
            try:
                return schema(data)
            except ValueError as e:
                raise error.ValueInvalid('not a valid value', path)
            except error.Invalid as e:
                e.prepend(path)
                raise

        return validate_callable

    def validate_value(path, data):
        if data != schema:
            raise error.ScalarInvalid('not a valid value', path)
        return data

    return validate_value


def _compile_itemsort():
    '''return sort function of mappings'''

    def is_extra(key_):
        return key_ is markers.Extra

    def is_remove(key_):
        return isinstance(key_, markers.Remove)

    def is_marker(key_):
        return isinstance(key_, markers.Marker)

    def is_type(key_):
        return inspect.isclass(key_)

    def is_callable(key_):
        return callable(key_)

    # priority list for map sorting (in order of checking)
    # We want Extra to match last, because it's a catch-all. On the other hand,
    # Remove markers should match first (since invalid values will not
    # raise an Error, instead the validator will check if other schemas match
    # the same value).
    priority = [(1, is_remove),  # Remove highest priority after values
                (2, is_marker),  # then other Markers
                (4, is_type),  # types/classes lowest before Extra
                (3, is_callable),  # callables after markers
                (5, is_extra)]  # Extra lowest priority

    def item_priority(item_):
        key_ = item_[0]
        for i, check_ in priority:
            if check_(key_):
                return i
        # values have hightest priorities
        return 0

    return item_priority


_sort_item = _compile_itemsort()


def _iterate_mapping_candidates(schema):
    """Iterate over schema in a meaningful order."""
    # Without this, Extra might appear first in the iterator, and fail to
    # validate a key even though it's a Required that has its own validation,
    # generating a false positive.
    return sorted(iteritems(schema), key=_sort_item)


def _iterate_object(obj):
    """Return iterator over object attributes. Respect objects with
    defined __slots__.

    """
    d = {}
    try:
        d = vars(obj)
    except TypeError:
        # maybe we have named tuple here?
        if hasattr(obj, '_asdict'):
            d = obj._asdict()
    for item in iteritems(d):
        yield item
    try:
        slots = obj.__slots__
    except AttributeError:
        pass
    else:
        for key in slots:
            if key != '__dict__':
                yield (key, getattr(obj, key))
    raise StopIteration()


class Msg(object):
    """Report a user-friendly message if a schema fails to validate.

    >>> validate = Schema(
    ...   Msg(['one', 'two', int],
    ...       'should be one of "one", "two" or an integer'))
    >>> with raises(MultipleInvalid, 'should be one of "one", "two" or an integer'):
    ...   validate(['three'])

    Messages are only applied to invalid direct descendants of the schema:

    >>> validate = Schema(Msg([['one', 'two', int]], 'not okay!'))
    >>> with raises(MultipleInvalid, 'expected int @ data[0][0]'):
    ...   validate([['three']])

    The type which is thrown can be overridden but needs to be a subclass of Invalid

    >>> with raises(SchemaError, 'Msg can only use subclases of Invalid as custom class'):
    ...   validate = Schema(Msg([int], 'should be int', cls=KeyError))

    If you do use a subclass of Invalid, that error will be thrown (wrapped in a MultipleInvalid)

    >>> validate = Schema(Msg([['one', 'two', int]], 'not okay!', cls=RangeInvalid))
    >>> try:
    ...  validate(['three'])
    ... except MultipleInvalid as e:
    ...   assert isinstance(e.errors[0], RangeInvalid)
    """

    def __init__(self, schema, msg, cls=None):
        if cls and not issubclass(cls, error.Invalid):
            raise error.SchemaError("Msg can only use subclases of"
                              " Invalid as custom class")
        self._schema = schema
        self.schema = Schema(schema)
        self.msg = msg
        self.cls = cls

    def __call__(self, v):
        try:
            return self.schema(v)
        except error.Invalid as e:
            if len(e.path) > 1:
                raise e
            else:
                raise (self.cls or error.Invalid)(self.msg)

    def __repr__(self):
        return 'Msg(%s, %s, cls=%s)' % (self._schema, self.msg, self.cls)
