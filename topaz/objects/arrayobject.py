import copy

from rpython.rlib import jit, objectmodel
from rpython.rlib.listsort import make_timsort_class
from rpython.rlib.rbigint import rbigint

from topaz.coerce import Coerce
from topaz.module import ClassDef, check_frozen
from topaz.modules.enumerable import Enumerable
from topaz.objects.objectobject import W_Object
from topaz.utils.packing.pack import RPacker

from rpython.rlib.objectmodel import import_from_mixin
import rstrategies as rstrat
from topaz.objects.objectobject import W_BaseObject
from topaz.objects.floatobject import W_FloatObject
from topaz.objects.intobject import W_FixnumObject

BaseRubySorter = make_timsort_class()
BaseRubySortBy = make_timsort_class()




class StrategyFactory(rstrat.StrategyFactory):
    _attrs_ = ['space', 'no_specialized_storage']
    _immutable_fields_ = ['space', 'no_specialized_storage[*]?']
    def __init__(self, space):
        self.space = space
        rstrat.StrategyFactory.__init__(self, AbstractStrategy)
        self.no_specialized_storage = [False]
    
    def instantiate_strategy(self, strategy_type, arr=None, initial_size=0):
        return strategy_type(self.space)
    
    def initialize_strategy(self, arr, elements_w):
        if self.no_specialized_storage[0]:
            typ = ObjectStrategy
        else:
            typ = self.strategy_type_for(elements_w)
        self.set_initial_strategy(arr, typ, len(elements_w), elements_w)
    
    @objectmodel.specialize.call_location()
    def log_string_for_object(self, obj):
        return ("%s" % obj).split(" ")[0] if obj else ""

class AbstractStrategy(object):
    __metaclass__ = rstrat.StrategyMetaclass
    import_from_mixin(rstrat.AbstractStrategy)
    import_from_mixin(rstrat.UnsafeIndexingMixin)
    _attrs_ = ['space']
    _immutable_fields_ = ['space']
    def __init__(self, space):
        self.space = space
    def strategy_factory(self):
        return self.space.strategy_factory
    
@rstrat.strategy()
class ObjectStrategy(AbstractStrategy):
    import_from_mixin(rstrat.GenericStrategy)
    def default_value(self): return self.space.w_nil
    
@rstrat.strategy(generalize=[ObjectStrategy])
class IntStrategy(AbstractStrategy):
    import_from_mixin(rstrat.SingleTypeStrategy)
    contained_type = W_FixnumObject
    def default_value(self): return self.wrap(0)
    def wrap(self, val): return self.space.newint_or_bigint(val)
    def unwrap(self, w_val): assert isinstance(w_val, W_FixnumObject); return w_val.intvalue
    
@rstrat.strategy(generalize=[ObjectStrategy])
class FloatStrategy(AbstractStrategy):
    import_from_mixin(rstrat.SingleTypeStrategy)
    contained_type = W_FloatObject
    def default_value(self): return self.wrap(0)
    def wrap(self, val): return self.space.newfloat(val)
    def unwrap(self, w_val): assert isinstance(w_val, W_FloatObject); return w_val.floatvalue

@rstrat.strategy(generalize=[
    FloatStrategy,
    IntStrategy,
    ObjectStrategy
])
class EmptyStrategy(AbstractStrategy):
    import_from_mixin(rstrat.EmptyStrategy)


class RubySorter(BaseRubySorter):
    def __init__(self, space, list, listlength=None, sortblock=None):
        BaseRubySorter.__init__(self, list, listlength=listlength)
        self.space = space
        self.sortblock = sortblock

    def lt(self, w_a, w_b):
        w_cmp_res = self.space.compare(w_a, w_b, self.sortblock)
        if self.space.is_kind_of(w_cmp_res, self.space.w_bignum):
            return self.space.bigint_w(w_cmp_res).lt(rbigint.fromint(0))
        else:
            return self.space.int_w(w_cmp_res) < 0


class RubySortBy(BaseRubySortBy):
    def __init__(self, space, list, listlength=None, sortblock=None):
        BaseRubySortBy.__init__(self, list, listlength=listlength)
        self.space = space
        self.sortblock = sortblock

    def lt(self, w_a, w_b):
        w_cmp_res = self.space.compare(
            self.space.invoke_block(self.sortblock, [w_a]),
            self.space.invoke_block(self.sortblock, [w_b])
        )
        return self.space.int_w(w_cmp_res) < 0


class W_ArrayObject(W_Object):
    classdef = ClassDef("Array", W_Object.classdef)
    classdef.include_module(Enumerable)
    rstrat.make_accessors(strategy='strategy', storage='_storage')
    strategy = None
    
    def __init__(self, space, items_w, klass=None):
        W_Object.__init__(self, space, klass)
        space.strategy_factory.initialize_strategy(self, items_w)

    def __deepcopy__(self, memo):
        obj = super(W_ArrayObject, self).__deepcopy__(memo)
        obj.strategy = self.strategy
        return obj

    def listview(self, space):
        return self.strategy.fetch_all(self)

    def length(self):
        return self.strategy.size(self)

    def size(self):
        return self.length()
        
    @classdef.singleton_method("allocate")
    def singleton_method_allocate(self, space):
        return W_ArrayObject(space, [], self)

    @classdef.method("initialize_copy", other_w="array")
    @classdef.method("replace", other_w="array")
    @check_frozen()
    def method_replace(self, space, other_w):
        self.strategy.delete(self, 0, self.size())
        self.strategy.append(self, other_w)
        return self

    @classdef.method("[]")
    @classdef.method("slice")
    def method_subscript(self, space, w_idx, w_count=None):
        start, end, as_range, nil = space.subscript_access(self.length(), w_idx, w_count=w_count)
        if nil:
            return space.w_nil
        elif as_range:
            assert start >= 0
            assert end >= 0
            return W_ArrayObject(space, self.strategy.slice(self, start, end), space.getnonsingletonclass(self))
        else:
            return self.strategy.fetch(self, start)

    @classdef.method("[]=")
    @check_frozen()
    def method_subscript_assign(self, space, w_idx, w_count_or_obj, w_obj=None):
        w_count = None
        if w_obj:
            w_count = w_count_or_obj
        else:
            w_obj = w_count_or_obj
        start, end, as_range, _ = space.subscript_access(self.length(), w_idx, w_count=w_count)

        if w_count and end < start:
            raise space.error(space.w_IndexError,
                "negative length (%d)" % (end - start)
            )
        elif start < 0:
            raise space.error(space.w_IndexError,
                "index %d too small for array; minimum: %d" % (
                    start - self.length(),
                    -self.length()
                )
            )
        elif as_range:
            w_converted = space.convert_type(w_obj, space.w_array, "to_ary", raise_error=False)
            if w_converted is space.w_nil:
                rep_w = [w_obj]
            else:
                rep_w = space.listview(w_converted)
            self._subscript_assign_range(space, start, end, rep_w)
        elif start >= self.length():
            self.strategy.append(self, [space.w_nil] * (start - self.length() + 1))
            self.strategy.store(self, start, w_obj)
        else:
            self.strategy.store(self, start, w_obj)
        return w_obj

    def _subscript_assign_range(self, space, start, end, rep_w):
        assert end >= 0
        delta = (end - start) - len(rep_w)
        if delta < 0:
            self.strategy.append(self, [space.w_nil] * -delta)
            lim = start + len(rep_w)
            i = self.length() - 1
            while i >= lim:
                self.strategy.store(self, i, self.strategy.fetch(self, i + delta))
                i -= 1
        elif delta > 0:
            self.strategy.delete(self, start, start + delta)
        for i in range(len(rep_w)):
            self.strategy.store(self, start + i, rep_w[i])

    @classdef.method("slice!")
    @check_frozen()
    def method_slice_i(self, space, w_idx, w_count=None):
        start, end, as_range, nil = space.subscript_access(self.length(), w_idx, w_count=w_count)

        if nil:
            return space.w_nil
        elif as_range:
            start = min(max(start, 0), self.length())
            end = min(max(end, 0), self.length())
            delta = (end - start)
            assert delta >= 0
            w_items = self.strategy.slice(self, start, start + delta)
            self.strategy.delete(self, start, start + delta)
            return space.newarray(w_items)
        else:
            w_item = self.strategy.fetch(self, start)
            self.strategy.delete(self, start, start+1)
            return w_item

    @classdef.method("size")
    @classdef.method("length")
    def method_length(self, space):
        return space.newint(self.length())

    @classdef.method("empty?")
    def method_emptyp(self, space):
        return space.newbool(self.length() == 0)

    @classdef.method("+", other="array")
    def method_add(self, space, other):
        return space.newarray(self.strategy.fetch_all(self) + other)

    @classdef.method("<<")
    @check_frozen()
    def method_lshift(self, space, w_obj):
        self.strategy.append(self, [w_obj])
        return self

    @classdef.method("concat", other="array")
    @check_frozen()
    def method_concat(self, space, other):
        self.strategy.append(self, other)
        return self

    @classdef.method("*")
    def method_times(self, space, w_other):
        if space.respond_to(w_other, "to_str"):
            return space.send(self, "join", [w_other])
        n = space.int_w(space.convert_type(w_other, space.w_fixnum, "to_int"))
        if n < 0:
            raise space.error(space.w_ArgumentError, "Count cannot be negative")
        w_res = W_ArrayObject(space, self.strategy.fetch_all(self) * n, space.getnonsingletonclass(self))
        space.infect(w_res, self, freeze=False)
        return w_res

    @classdef.method("push")
    @check_frozen()
    def method_push(self, space, args_w):
        self.strategy.append(self, args_w)
        return self

    @classdef.method("shift")
    @check_frozen()
    def method_shift(self, space, w_n=None):
        if w_n is None:
            if self.size() > 0:
                return self.strategy.pop(self, 0)
            else:
                return space.w_nil
        n = space.int_w(space.convert_type(w_n, space.w_fixnum, "to_int"))
        if n < 0:
            raise space.error(space.w_ArgumentError, "negative array size")
        items_w = self.strategy.slice(self, 0, n)
        self.strategy.delete(self, 0, n)
        return space.newarray(items_w)

    @classdef.method("unshift")
    @check_frozen()
    def method_unshift(self, space, args_w):
        for w_obj in reversed(args_w):
            self.strategy.insert(self, 0, [w_obj])
        return self

    @classdef.method("join")
    def method_join(self, space, w_sep=None):
        if self.size() == 0:
            return space.newstr_fromstr("")
        if w_sep is None:
            separator = ""
        elif space.respond_to(w_sep, "to_str"):
            separator = space.str_w(space.send(w_sep, "to_str"))
        else:
            raise space.error(space.w_TypeError,
                "can't convert %s into String" % space.getclass(w_sep).name
            )
        return space.newstr_fromstr(separator.join([
            space.str_w(space.send(w_o, "to_s"))
            for w_o in self.strategy.fetch_all(self)
        ]))

    @classdef.method("pop")
    @check_frozen()
    def method_pop(self, space, w_num=None):
        if w_num is None:
            if self.size() > 0:
                return self.strategy.pop(self, self.size() - 1)
            else:
                return space.w_nil
        else:
            num = space.int_w(space.convert_type(
                w_num, space.w_fixnum, "to_int"
            ))
            if num < 0:
                raise space.error(space.w_ArgumentError, "negative array size")
            else:
                pop_size = max(0, self.length() - num)
                res_w = self.strategy.slice(self, pop_size, self.size())
                self.strategy.delete(self, pop_size, self.size())
                return space.newarray(res_w)

    @classdef.method("delete_at", idx="int")
    @check_frozen()
    def method_delete_at(self, space, idx):
        if idx < 0:
            idx += self.length()
        if idx < 0 or idx >= self.length():
            return space.w_nil
        else:
            return self.strategy.pop(self, idx)

    @classdef.method("last")
    def method_last(self, space, w_count=None):
        if w_count is not None:
            count = Coerce.int(space, w_count)
            if count < 0:
                raise space.error(space.w_ArgumentError, "negative array size")
            start = self.length() - count
            if start < 0:
                start = 0
            return space.newarray(self.strategy.slice(self, start, self.size()))

        if self.length() == 0:
            return space.w_nil
        else:
            return self.strategy.fetch(self, self.length() - 1)

    @classdef.method("pack")
    def method_pack(self, space, w_template):
        template = Coerce.str(space, w_template)
        result = RPacker(template, space.listview(self)).operate(space)
        w_result = space.newstr_fromchars(result)
        space.infect(w_result, w_template)
        return w_result

    @classdef.method("to_ary")
    def method_to_ary(self, space):
        return self

    @classdef.method("clear")
    @check_frozen()
    def method_clear(self, space):
        self.strategy.delete(self, 0, self.size())
        return self

    @classdef.method("sort!")
    @check_frozen()
    def method_sort_i(self, space, block):
        #RubySorter(space, self.items_w, sortblock=block).sort()
        #return self
        raise NotImplementedError("sort!")

    @classdef.method("sort_by!")
    @check_frozen()
    def method_sort_by_i(self, space, block):
        if block is None:
            return space.send(self, "enum_for", [space.newsymbol("sort_by!")])
        #RubySortBy(space, self.items_w, sortblock=block).sort()
        #return self
        raise NotImplementedError("sort!")

    @classdef.method("reverse!")
    @check_frozen()
    def method_reverse_i(self, space):
        #self.items_w.reverse()
        #return self
        raise NotImplementedError("sort!")

    @classdef.method("rotate!", n="int")
    @check_frozen()
    def method_rotate_i(self, space, n=1):
        length = self.length()
        if length == 0:
            return self
        if abs(n) >= length:
            n %= length
        if n < 0:
            n += length
        if n == 0:
            return self
        assert n >= 0
        self.strategy.append(self, self.strategy.slice(self, 0, n))
        self.strategy.delete(self, 0, n)
        return self

    @classdef.method("insert", i="int")
    @check_frozen()
    @jit.look_inside_iff(lambda self, space, i, args_w: jit.isconstant(len(args_w)))
    def method_insert(self, space, i, args_w):
        if not args_w:
            return self
        length = self.length()
        if i > length:
            self._append_nils(space, i - length)
            self.strategy.append(self, args_w)
            return self
        if i < 0:
            if i < -length - 1:
                raise space.error(space.w_IndexError,
                    "index %d too small for array; minimum: %d" % (i + 1, -length)
                )
            i += length + 1
        assert i >= 0
        for w_e in args_w:
            self.strategy.insert(self, i, [w_e])
            i += 1
        return self

    def _append_nils(self, space, num):
        for _ in xrange(num):
            self.strategy.append(self, [space.w_nil])
