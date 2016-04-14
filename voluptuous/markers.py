from error import Invalid, SchemaError


class Undefined(object):
    def __nonzero__(self):
        return False

    def __repr__(self):
        return '...'


UNDEFINED = Undefined()


def default_factory(value):
    if value is UNDEFINED or callable(value):
        return value
    return lambda: value


class Marker(object):
    """Mark nodes for special treatment."""

    def __init__(self, schema, msg=None):
        self.schema = schema
        self._schema = schema.Schema(schema)
        self.msg = msg

    def __call__(self, v):
        try:
            return self._schema(v)
        except Invalid as e:
            if not self.msg or len(e.path) > 1:
                raise
            raise Invalid(self.msg)

    def __str__(self):
        return str(self.schema)

    def __repr__(self):
        return repr(self.schema)

    def __lt__(self, other):
        return self.schema < other.schema


class Optional(Marker):
    """Mark a node in the schema as optional, and optionally provide a default

    >>> schema = Schema({Optional('key'): str})
    >>> schema({})
    {}
    >>> schema = Schema({Optional('key', default='value'): str})
    >>> schema({})
    {'key': 'value'}
    >>> schema = Schema({Optional('key', default=list): list})
    >>> schema({})
    {'key': []}

    If 'required' flag is set for an entire schema, optional keys aren't required

    >>> schema = Schema({
    ...    Optional('key'): str,
    ...    'key2': str
    ... }, required=True)
    >>> schema({'key2':'value'})
    {'key2': 'value'}
    """

    def __init__(self, schema, msg=None, default=UNDEFINED):
        super(Optional, self).__init__(schema, msg=msg)
        self.default = default_factory(default)


class Exclusive(Optional):
    """Mark a node in the schema as exclusive.

    Exclusive keys inherited from Optional:

    >>> schema = Schema({Exclusive('alpha', 'angles'): int, Exclusive('beta', 'angles'): int})
    >>> schema({'alpha': 30})
    {'alpha': 30}

    Keys inside a same group of exclusion cannot be together, it only makes sense for dictionaries:

    >>> with raises(MultipleInvalid, "two or more values in the same group of exclusion 'angles' @ data[<angles>]"):
    ...   schema({'alpha': 30, 'beta': 45})

    For example, API can provides multiple types of authentication, but only one works in the same time:

    >>> msg = 'Please, use only one type of authentication at the same time.'
    >>> schema = Schema({
    ... Exclusive('classic', 'auth', msg=msg):{
    ...     Required('email'): basestring,
    ...     Required('password'): basestring
    ...     },
    ... Exclusive('internal', 'auth', msg=msg):{
    ...     Required('secret_key'): basestring
    ...     },
    ... Exclusive('social', 'auth', msg=msg):{
    ...     Required('social_network'): basestring,
    ...     Required('token'): basestring
    ...     }
    ... })

    >>> with raises(MultipleInvalid, "Please, use only one type of authentication at the same time. @ data[<auth>]"):
    ...     schema({'classic': {'email': 'foo@example.com', 'password': 'bar'},
    ...             'social': {'social_network': 'barfoo', 'token': 'tEMp'}})
    """

    def __init__(self, schema, group_of_exclusion, msg=None):
        super(Exclusive, self).__init__(schema, msg=msg)
        self.group_of_exclusion = group_of_exclusion


class Inclusive(Optional):
    """ Mark a node in the schema as inclusive.

    Exclusive keys inherited from Optional:

    >>> schema = Schema({
    ...     Inclusive('filename', 'file'): str,
    ...     Inclusive('mimetype', 'file'): str
    ... })
    >>> data = {'filename': 'dog.jpg', 'mimetype': 'image/jpeg'}
    >>> data == schema(data)
    True

    Keys inside a same group of inclusive must exist together, it only makes sense for dictionaries:

    >>> with raises(MultipleInvalid, "some but not all values in the same group of inclusion 'file' @ data[<file>]"):
    ...     schema({'filename': 'dog.jpg'})

    If none of the keys in the group are present, it is accepted:

    >>> schema({})
    {}

    For example, API can return 'height' and 'width' together, but not separately.

    >>> msg = "Height and width must exist together"
    >>> schema = Schema({
    ...     Inclusive('height', 'size', msg=msg): int,
    ...     Inclusive('width', 'size', msg=msg): int
    ... })

    >>> with raises(MultipleInvalid, msg + " @ data[<size>]"):
    ...     schema({'height': 100})

    >>> with raises(MultipleInvalid, msg + " @ data[<size>]"):
    ...     schema({'width': 100})

    >>> data = {'height': 100, 'width': 100}
    >>> data == schema(data)
    True
    """

    def __init__(self, schema, group_of_inclusion, msg=None):
        super(Inclusive, self).__init__(schema, msg=msg)
        self.group_of_inclusion = group_of_inclusion


class Required(Marker):
    """Mark a node in the schema as being required, and optionally provide a default value.

    >>> schema = Schema({Required('key'): str})
    >>> with raises(MultipleInvalid, "required key not provided @ data['key']"):
    ...   schema({})

    >>> schema = Schema({Required('key', default='value'): str})
    >>> schema({})
    {'key': 'value'}
    >>> schema = Schema({Required('key', default=list): list})
    >>> schema({})
    {'key': []}
    """

    def __init__(self, schema, msg=None, default=UNDEFINED):
        super(Required, self).__init__(schema, msg=msg)
        self.default = default_factory(default)


class Remove(Marker):
    """Mark a node in the schema to be removed and excluded from the validated
    output. Keys that fail validation will not raise ``Invalid``. Instead, these
    keys will be treated as extras.

    >>> schema = Schema({str: int, Remove(int): str})
    >>> with raises(MultipleInvalid, "extra keys not allowed @ data[1]"):
    ...    schema({'keep': 1, 1: 1.0})
    >>> schema({1: 'red', 'red': 1, 2: 'green'})
    {'red': 1}
    >>> schema = Schema([int, Remove(float), Extra])
    >>> schema([1, 2, 3, 4.0, 5, 6.0, '7'])
    [1, 2, 3, 5, '7']
    """

    def __call__(self, v):
        super(Remove, self).__call__(v)
        return self.__class__

    def __repr__(self):
        return "Remove(%r)" % (self.schema,)


def Extra(_):
    """Allow keys in the data that are not present in the schema."""
    raise SchemaError('"Extra" should never be called')


# As extra() is never called there's no way to catch references to the
# deprecated object, so we just leave an alias here instead.
extra = Extra
