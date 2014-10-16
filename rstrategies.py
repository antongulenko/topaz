
import weakref
import rstrategies_logger
from rpython.rlib import jit, objectmodel

class StrategyMetaclass(type):
    def __new__(self, name, bases, attrs):
        attrs['_is_strategy'] = False
        attrs['_specializations'] = []
        return super(StrategyMetaclass, self).__new__(self, name, bases, attrs)

def collect_subclasses(cls):
    "NOT_RPYTHON"
    subclasses = []
    for subcls in cls.__subclasses__():
        subclasses.append(subcls)
        subclasses.extend(collect_subclasses(subcls))
    return subclasses

class StrategyFactory(object):
    _immutable_fields_ = ["strategies[*]", "logger"]
    
    def __init__(self, root_class, all_strategy_classes=None):
        if all_strategy_classes is None:
            all_strategy_classes = collect_subclasses(root_class)
        self.strategies = []
        self.logger = rstrategies_logger.Logger()
        
        for strategy_class in all_strategy_classes:
            if hasattr(strategy_class, "_is_strategy") and strategy_class._is_strategy:
                strategy_class._strategy_instance = self.instantiate_empty(strategy_class)
                self.strategies.append(strategy_class)
            self.patch_strategy_class(strategy_class, root_class)
        self.order_strategies()
    
    def patch_strategy_class(self, strategy_class, root_class):
        # Patch root class: Add default handler for visitor
        def copy_from_OTHER(self, other):
            self.copy_from(other)
        funcname = "copy_from_" + strategy_class.__name__
        copy_from_OTHER.func_name = funcname
        setattr(root_class, funcname, copy_from_OTHER)
        
        # Patch strategy class: Add polymorphic visitor function
        def initiate_copy_into(self, other):
            getattr(other, funcname)(self)
        strategy_class.initiate_copy_into = initiate_copy_into
    
    def decorate_strategies(self, transitions):
        "NOT_RPYTHON"
        for strategy_class, generalized in transitions.items():
            strategy(generalized)(strategy_class)
    
    def order_strategies(self):
        "NOT_RPYTHON"
        def get_generalization_depth(strategy, visited=None):
            if visited is None:
                visited = set()
            if strategy._generalizations:
                if strategy in visited:
                    raise Exception("Cycle in generalization-tree of %s" % strategy)
                visited.add(strategy)
                depth = 0
                for generalization in strategy._generalizations:
                    other_depth = get_generalization_depth(generalization, visited)
                    depth = max(depth, other_depth)
                return depth + 1
            else:
                return 0
        self.strategies.sort(key=get_generalization_depth, reverse=True)
    
    # Instantiate new_strategy_type with size, replace old_strategy with it,
    # and return the new instance
    def instantiate_and_switch(self, old_strategy, size, new_strategy_type):
        raise NotImplementedError("Abstract method")
    
    # Return a functional but empty instance of strategy_type
    def instantiate_empty(self, strategy_type):
        raise NotImplementedError("Abstract method")
    
    # This can be overwritten into a more appropriate call to self.logger.log
    def log(self, new_strategy, old_strategy=None, new_element=None):
        if not self.logger.active: return
        new_strategy_str = self.log_string_for_object(new_strategy)
        old_strategy_str = self.log_string_for_object(old_strategy)
        element_typename = self.log_string_for_object(new_element)
        size = new_strategy.size()
        typename = ""
        cause = "Switched" if old_strategy else "Created"
        self.logger.log(new_strategy_str, size, cause, old_strategy_str, typename, element_typename)
        
    @objectmodel.specialize.call_location()
    def log_string_for_object(self, obj):
        return obj.__class__.__name__ if obj else ""
    
    def switch_strategy(self, old_strategy, new_strategy_type, new_element=None):
        new_instance = self.instantiate_and_switch(old_strategy, old_strategy.size(), new_strategy_type)
        old_strategy.initiate_copy_into(new_instance)
        new_instance.strategy_switched()
        self.log(new_instance, old_strategy, new_element)
        return new_instance
    
    @jit.unroll_safe
    def strategy_type_for(self, objects):
        specialized_strategies = len(self.strategies)
        can_handle = [True] * specialized_strategies
        for obj in objects:
            if specialized_strategies <= 1:
                break
            for i, strategy in enumerate(self.strategies):
                if can_handle[i] and not strategy._strategy_instance.check_can_handle(obj):
                    can_handle[i] = False
                    specialized_strategies -= 1
        for i, strategy_type in enumerate(self.strategies):
            if can_handle[i]:
                return strategy_type
        raise Exception("Could not find strategy to handle: %s" % objects)
    
    def _freeze_(self):
        # Instance will be frozen at compile time, making accesses constant.
        return True

def strategy(generalize=None):
    def decorator(strategy_class):
        # Patch strategy class: Add generalized_strategy_for and mark as strategy class.
        if generalize:
            # TODO - optimize this method
            @jit.unroll_safe
            def generalized_strategy_for(self, value):
                for strategy in generalize:
                    if strategy._strategy_instance.check_can_handle(value):
                        return strategy
                raise Exception("Could not find generalized strategy for %s coming from %s" % (value, self))
            strategy_class.generalized_strategy_for = generalized_strategy_for
            for generalized in generalize:
                generalized._specializations.append(strategy_class)
        strategy_class._is_strategy = True
        strategy_class._generalizations = generalize
        return strategy_class
    return decorator

class AbstractCollection(object):
    # == Required:
    # store(self, n0, e)
    
    def strategy_switched(self): pass
    def init_strategy(self, initial_size): pass
    
    def initiate_copy_into(self, other):
        other.copy_from(self)
    
    def copy_from(self, other):
        assert self.size() == other.size()
        for i in range(self.size()):
            self.copy_field_from(i, other)
    
    def copy_field_from(self, n0, other):
        self.store(n0, other.fetch(n0))
    
class AbstractStrategy(object):
    # == Required:
    # strategy_factory(self) - Access to StorageFactory
    # __init__(...) - Constructor should invoke the provided init_strategy(self, size) method
    
    # Main Fixedsize API
    
    def store(self, index0, value):
        raise NotImplementedError("Abstract method")
    
    def fetch(self, index0):
        raise NotImplementedError("Abstract method")
    
    def size(self):
        raise NotImplementedError("Abstract method")
    
    # Fixedsize utility methods
    
    def slice(self, start, end):
        return [ self.fetch(i) for i in range(start, end)]
    
    def fetch_all(self):
        return self.slice(0, self.size())
    
    # Main Varsize API
    
    def insert(self, index0, list_w):
        raise NotImplementedError("Abstract method")
    
    def delete(self, start, end):
        raise NotImplementedError("Abstract method")
    
    # Varsize utility methods
    
    def append(self, list_w):
        self.insert(self.size(), list_w)        
    
    def pop(self, index0):
        e = self.fetch(index0)
        self.delete(index0, index0+1)
        return e

    # Internal methods
    
    def check_can_handle(self, value):
        raise NotImplementedError("Abstract method")
    
    def generalize_for_value(self, value):
        strategy_type = self.generalized_strategy_for(value)
        new_instance = self.strategy_factory().switch_strategy(self, strategy_type, new_element=value)
        return new_instance
        
    def cannot_handle_store(self, index0, value):
        new_instance = self.generalize_for_value(value)
        new_instance.store(index0, value)
        
    def cannot_handle_insert(self, index0, list_w):
        # TODO - optimize. Prevent multiple generalizations and slicing done by callers.
        new_strategy = self.generalize_for_value(list_w[0])
        new_strategy.insert(index0, list_w)

# ============== Special Strategies with no storage array ==============

class EmptyStrategy(AbstractStrategy):
    # == Required:
    # See AbstractStrategy
    
    def fetch(self, index0):
        raise IndexError
    def store(self, index0, value):
        self.cannot_handle_insert(index0, [value])
    def insert(self, index0, list_w):
        self.cannot_handle_insert(index0, list_w)
    def delete(self, start, end):
        self.check_index_range(start, end)
    def size(self):
        return 0
    def check_can_handle(self, value):
        return False
    
class SingleValueStrategy(AbstractStrategy):
    _immutable_fields_ = ["_size"]
    _attrs_ = ["_size"]
    # == Required:
    # See AbstractStrategy
    # check_index_*(...) - use mixin SafeIndexingMixin or UnsafeIndexingMixin
    # value(self) - the single value contained in this strategy
    
    def init_strategy(self, initial_size):
        self._size = initial_size
    def fetch(self, index0):
        self.check_index_fetch(index0)
        return self.value()
    def store(self, index0, value):
        self.check_index_store(index0)
        if self.check_can_handle(value):
            return
        self.cannot_handle_store(index0, value)
    
    @jit.unroll_safe
    def insert(self, index0, list_w):
        for i in range(len(list_w)):
            if self.check_can_handle(list_w[handled]):
                self._size += 1
            else:
                self.cannot_handle_insert(index0 + i, list_w[i:])
                return
    
    def delete(self, start, end):
        self.check_index_range(start, end)
        self._size -= (end - start)
    def size(self):
        return self._size
    def check_can_handle(self, value):
        return value is self.value()
    
# ============== Basic strategies with storage ==============

class StrategyWithStorage(AbstractStrategy):
    _immutable_fields_ = ["storage"]
    _attrs_ = ["storage"]
    # == Required:
    # See AbstractStrategy
    # check_index_*(...) - use mixin SafeIndexingMixin or UnsafeIndexingMixin
    # default_value(self) - The value to be initially contained in this strategy
    
    def init_strategy(self, initial_size):
        default = self._unwrap(self.default_value())
        self.storage = [default] * initial_size
    
    def store(self, index0, wrapped_value):
        self.check_index_store(index0)
        if self.check_can_handle(wrapped_value):
            unwrapped = self._unwrap(wrapped_value)
            self.storage[index0] = unwrapped
        else:
            self.cannot_handle_store(index0, wrapped_value)
    
    def fetch(self, index0):
        self.check_index_fetch(index0)
        unwrapped = self.storage[index0]
        return self._wrap(unwrapped)
    
    def _wrap(self, value):
        raise NotImplementedError("Abstract method")
    
    def _unwrap(self, value):
        raise NotImplementedError("Abstract method")
    
    def size(self):
        return len(self.storage)
    
    @jit.unroll_safe
    def insert(self, start, list_w):
        if start > self.size():
            start = self.size()
        for i in range(len(list_w)):
            if self.check_can_handle(list_w[i]):
                self.storage.insert(start + i, self._unwrap(list_w[i]))
            else:
                self.cannot_handle_insert(start + i, list_w[i:])
                return
    
    def delete(self, start, end):
        self.check_index_range(start, end)
        assert start >= 0 and end >= 0
        del self.storage[start : end]
    
class GenericStrategy(StrategyWithStorage):
    # == Required:
    # See StrategyWithStorage
    
    def _wrap(self, value):
        return value
    def _unwrap(self, value):
        return value
    def check_can_handle(self, wrapped_value):
        return True
    
class WeakGenericStrategy(StrategyWithStorage):
    # == Required:
    # See StrategyWithStorage
    
    def _wrap(self, value):
        return value() or self.default_value()
    def _unwrap(self, value):
        assert value is not None
        return weakref.ref(value)
    def check_can_handle(self, wrapped_value):
        return True
    
# ============== Mixins for StrategyWithStorage ==============

class SafeIndexingMixin(object):
    def check_index_store(self, index0):
        self.check_index(index0)
    def check_index_fetch(self, index0):
        self.check_index(index0)
    def check_index_range(self, start, end):
        if end < start:
            raise IndexError
        self.check_index(start)
        self.check_index(end)
    def check_index(self, index0):
        if index0 < 0 or index0 >= self.size():
            raise IndexError

class UnsafeIndexingMixin(object):
    def check_index_store(self, index0):
        pass
    def check_index_fetch(self, index0):
        pass
    def check_index_range(self, start, end):
        pass

# ============== Specialized Storage Strategies ==============

class SpecializedStrategy(StrategyWithStorage):
    # == Required:
    # See StrategyWithStorage
    # wrap(self, value) - Return a boxed object for the primitive value
    # unwrap(self, value) - Return the unboxed primitive value of value
    
    def _unwrap(self, value):
        return self.unwrap(value)
    def _wrap(self, value):
        return self.wrap(value)
    
class SingleTypeStrategy(SpecializedStrategy):
    # == Required Functions:
    # See SpecializedStrategy
    # contained_type - The wrapped type that can be stored in this strategy
    
    def check_can_handle(self, value):
        return isinstance(value, self.contained_type)
    
class TaggingStrategy(SingleTypeStrategy):
    """This strategy uses a special tag value to represent a single additional object."""
    # == Required:
    # See SingleTypeStrategy
    # wrapped_tagged_value(self) - The tagged object
    # unwrapped_tagged_value(self) - The unwrapped tag value representing the tagged object
    
    def check_can_handle(self, value):
        return value is self.wrapped_tagged_value() or \
                (isinstance(value, self.contained_type) and \
                self.unwrap(value) != self.unwrapped_tagged_value())
    
    def _unwrap(self, value):
        if value is self.wrapped_tagged_value():
            return self.unwrapped_tagged_value()
        return self.unwrap(value)
    
    def _wrap(self, value):
        if value == self.unwrapped_tagged_value():
            return self.wrapped_tagged_value()
        return self.wrap(value)
