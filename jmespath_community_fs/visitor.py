import operator

from jmespath_community_fs import exceptions
from jmespath_community_fs import functions
from jmespath_community_fs.compat import string_type
from jmespath_community_fs.scope import ScopedChainDict
from numbers import Number


def _equals(x, y):
    if _is_special_number_case(x, y):
        return False
    else:
        return x == y


def _is_special_number_case(x, y):
    # We need to special case comparing 0 or 1 to
    # True/False.  While normally comparing any
    # integer other than 0/1 to True/False will always
    # return False.  However 0/1 have this:
    # >>> 0 == True
    # False
    # >>> 0 == False
    # True
    # >>> 1 == True
    # True
    # >>> 1 == False
    # False
    #
    # Also need to consider that:
    # >>> 0 in [True, False]
    # True
    if _is_actual_number(x) and x in (0, 1):
        return isinstance(y, bool)
    elif _is_actual_number(y) and y in (0, 1):
        return isinstance(x, bool)


def _is_comparable(x):
    # The spec doesn't officially support string types yet,
    # but enough people are relying on this behavior that
    # it's been added back.  This should eventually become
    # part of the official spec.
    return _is_actual_number(x) or isinstance(x, string_type)


def _is_actual_number(x):
    # We need to handle python's quirkiness with booleans,
    # specifically:
    #
    # >>> isinstance(False, int)
    # True
    # >>> isinstance(True, int)
    # True
    if isinstance(x, bool):
        return False
    return isinstance(x, Number)


class Options(object):
    """Options to control how a JMESPath function is evaluated."""
    def __init__(self, dict_cls=None,
        custom_functions=None,
        enable_legacy_literals=False):

        #: The class to use when creating a dict.  The interpreter
        #  may create dictionaries during the evaluation of a JMESPath
        #  expression.  For example, a multi-select hash will
        #  create a dictionary.  By default we use a dict() type.
        #  You can set this value to change what dict type is used.
        #  The most common reason you would change this is if you
        #  want to set a collections.OrderedDict so that you can
        #  have predictable key ordering.
        self.dict_cls = dict_cls
        self.custom_functions = custom_functions

        #: The flag to enable pre-JEP-12 literal compatibility.
        #  JEP-12 deprecates `foo` -> "foo" syntax.
        #  Valid expressions MUST use: `"foo"` -> "foo"
        #  Setting this flag to `True` enables support for legacy syntax.
        self.enable_legacy_literals = enable_legacy_literals


class _Expression(object):
    def __init__(self, expression, interpreter):
        self.expression = expression
        self.interpreter = interpreter

    def visit(self, node, *args, **kwargs):
        return self.interpreter.visit(node, *args, **kwargs)

class Visitor(object):
    def __init__(self):
        self._method_cache = {}

    def visit(self, node, *args, **kwargs):
        node_type = node['type']
        method = self._method_cache.get(node_type)
        if method is None:
            method = getattr(
                self, 'visit_%s' % node['type'], self.default_visit)
            self._method_cache[node_type] = method
        return method(node, *args, **kwargs)

    def default_visit(self, node, *args, **kwargs):
        raise NotImplementedError("default_visit")

class TreeInterpreter(Visitor):
    COMPARATOR_FUNC = {
        'eq': _equals,
        'ne': lambda x, y: not _equals(x, y),
        'lt': operator.lt,
        'gt': operator.gt,
        'lte': operator.le,
        'gte': operator.ge
    }
    _EQUALITY_OPS = ['eq', 'ne']
    _ARITHMETIC_UNARY_FUNC = {
        'minus': operator.neg,
        'plus': lambda x: x
    }
    _ARITHMETIC_FUNC = {
        'div': operator.floordiv,
        'divide': operator.truediv,
        'minus': operator.sub,
        'modulo': operator.mod,
        'multiply': operator.mul,
        'plus': operator.add,
    }
    MAP_TYPE = dict

    def __init__(self, options=None):
        super(TreeInterpreter, self).__init__()
        self._dict_cls = self.MAP_TYPE
        if options is None:
            options = Options()
        self._options = options
        if options.dict_cls is not None:
            self._dict_cls = self._options.dict_cls
        if options.custom_functions is not None:
            self._functions = self._options.custom_functions
        else:
            self._functions = functions.Functions()
        self._root = None
        self._scope = ScopedChainDict()

    def default_visit(self, node, *args, **kwargs):
        raise NotImplementedError(node['type'])

    def evaluate(self, ast, root):
        self._root = root
        return self.visit(ast, root)

    def visit_subexpression(self, node, value):
        result = value
        for node in node['children']:
            result = self.visit(node, result)
            if (result is None):
                return None
        return result

    def visit_field(self, node, value, *args, **kwargs):
        try:
           return value.get(node['value']) 
        except AttributeError:
            return None

    def visit_comparator(self, node, value):
        # Common case: comparator is == or !=
        comparator_func = self.COMPARATOR_FUNC[node['value']]
        if node['value'] in self._EQUALITY_OPS:
            return comparator_func(
                self.visit(node['children'][0], value),
                self.visit(node['children'][1], value)
            )
        else:
            # Ordering operators are only valid for numbers.
            # Evaluating any other type with a comparison operator
            # will yield a None value.
            left = self.visit(node['children'][0], value)
            right = self.visit(node['children'][1], value)
            num_types = (int, float)
            if not (_is_comparable(left) and
                    _is_comparable(right)):
                return None
            return comparator_func(left, right)

    def visit_arithmetic_unary(self, node, value):
        operation = self._ARITHMETIC_UNARY_FUNC[node['value']]
        return operation(
            self.visit(node['children'][0], value)
        )

    def visit_arithmetic(self, node, value):
        operation = self._ARITHMETIC_FUNC[node['value']]
        return operation(
            self.visit(node['children'][0], value),
            self.visit(node['children'][1], value)
        )

    def visit_current(self, node, value):
        return value

    def visit_root(self, node, value):
        return self._root

    def visit_expref(self, node, value):
        return _Expression(node['children'][0], self)

    def visit_function_expression(self, node, value, *args, **kwargs):
        resolved_args = []
        for child in node['children']:
            current = self.visit(child, value)
            resolved_args.append(current)
        return self._functions.call_function(node['value'], resolved_args, scopes = kwargs.get('scopes'))

    def visit_filter_projection(self, node, value):
        base = self.visit(node['children'][0], value)
        if not isinstance(base, list):
            return None
        comparator_node = node['children'][2]
        collected = []
        for element in base:
            if self._is_true(self.visit(comparator_node, element)):
                current = self.visit(node['children'][1], element)
                if current is not None:
                    collected.append(current)
        return collected

    def visit_flatten(self, node, value):
        base = self.visit(node['children'][0], value)
        if not isinstance(base, list):
            # Can't flatten the object if it's not a list.
            return None
        merged_list = []
        for element in base:
            if isinstance(element, list):
                merged_list.extend(element)
            else:
                merged_list.append(element)
        return merged_list

    def visit_identity(self, node, value):
        return value

    def visit_index(self, node, value):
        # Even though we can index strings, we don't
        # want to support that.
        if not isinstance(value, list):
            return None
        try:
            return value[node['value']]
        except IndexError:
            return None

    def visit_index_expression(self, node, value):
        result = value
        for node in node['children']:
            result = self.visit(node, result)
        return result

    def visit_slice(self, node, value):
        if isinstance(value, string_type):
            start = node['children'][0]
            end = node['children'][1]
            step = node['children'][2]
            return value[start:end:step]

        if not isinstance(value, list):
            return None
        s = slice(*node['children'])
        return value[s]

    def visit_key_val_pair(self, node, value):
        return self.visit(node['children'][0], value)

    def visit_literal(self, node, value):
        return node['value']

    def visit_multi_select_dict(self, node, value):
        collected = self._dict_cls()
        for child in node['children']:
            collected[child['value']] = self.visit(child, value)
        return collected

    def visit_multi_select_list(self, node, value):
        collected = []
        for child in node['children']:
            collected.append(self.visit(child, value))
        return collected

    def visit_or_expression(self, node, value):
        matched = self.visit(node['children'][0], value)
        if self._is_false(matched):
            matched = self.visit(node['children'][1], value)
        return matched

    def visit_and_expression(self, node, value):
        matched = self.visit(node['children'][0], value)
        if self._is_false(matched):
            return matched
        return self.visit(node['children'][1], value)

    def visit_not_expression(self, node, value):
        original_result = self.visit(node['children'][0], value)
        if _is_actual_number(original_result) and original_result == 0:
            # Special case for 0, !0 should be false, not true.
            # 0 is not a special cased integer in jmespath.
            return False
        return not original_result

    def visit_pipe(self, node, value):
        result = value
        for node in node['children']:
            result = self.visit(node, result)
        return result

    def visit_projection(self, node, value):
        base = self.visit(node['children'][0], value)

        allow_string = False
        first_child = node['children'][0]
        if first_child['type'] == 'index_expression':
            nested_children = first_child['children']
            if len(nested_children) > 1 and nested_children[1]['type'] == 'slice':
                allow_string = True

        if isinstance(base, string_type) and allow_string:
            ## projections are really sub-expressions in disguise
            ## evaluate the rhs when lhs is a sliced string
            return self.visit(node['children'][1], base)

        if not isinstance(base, list):
            return None
        collected = []
        for element in base:
            current = self.visit(node['children'][1], element)
            if current is not None:
                collected.append(current)
        return collected

    def visit_let_expression(self, node, value):
        *bindings, expr = node['children']
        scope = {}
        for assign in bindings:
            scope.update(self.visit(assign, value))
        self._scope.push_scope(scope)
        result = self.visit(expr, value)
        self._scope.pop_scope()
        return result

    def visit_assign(self, node, value):
        name = node['value']
        value = self.visit(node['children'][0], value)
        return {name: value}

    def visit_variable_ref(self, node, value):
        try:
            return self._scope[node['value']]
        except KeyError:
            raise exceptions.UndefinedVariable(node['value'])

    def visit_ternary_operator(self, node, value):
        condition = node['children'][0]
        evaluation = self.visit(condition, value)

        if self._is_false(evaluation):
            falsyNode = node['children'][2]
            return self.visit(falsyNode, value)
        else:
            truthyNode = node['children'][1]
            return self.visit(truthyNode, value)

    def visit_value_projection(self, node, value):
        base = self.visit(node['children'][0], value)
        try:
            base = base.values()
        except AttributeError:
            return None
        collected = []
        for element in base:
            current = self.visit(node['children'][1], element)
            if current is not None:
                collected.append(current)
        return collected

    def _is_false(self, value):
        # This looks weird, but we're explicitly using equality checks
        # because the truth/false values are different between
        # python and jmespath.
        return (value == '' or value == [] or value == {} or value is None or
                value is False)

    def _is_true(self, value):
        return not self._is_false(value)


class GraphvizVisitor(Visitor):
    def __init__(self):
        super(GraphvizVisitor, self).__init__()
        self._lines = []
        self._count = 1

    def visit(self, node, *args, **kwargs):
        self._lines.append('digraph AST {')
        current = '%s%s' % (node['type'], self._count)
        self._count += 1
        self._visit(node, current)
        self._lines.append('}')
        return '\n'.join(self._lines)

    def _visit(self, node, current):
        self._lines.append('%s [label="%s(%s)"]' % (
            current, node['type'], node.get('value', '')))
        for child in node.get('children', []):
            child_name = '%s%s' % (child['type'], self._count)
            self._count += 1
            self._lines.append('  %s -> %s' % (current, child_name))
            self._visit(child, child_name)
