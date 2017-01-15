from . import token as K
from . import types as T
from . import module as M
from .ast import *
from collections import OrderedDict
from llvmlite import ir
import sys

BOX_SIZE = 24
HASH_SIZE = 32

# structure

@program_node.method
def emit(self, module):
  module.metatable_key = module.add_global(T.box)
  module.metatable_key.initializer = str_node('metatable').emit(module)

  module.exports = module.add_global(T.box, name=module.mangle('exports'))
  module.exports.initializer = static_table_alloc(module, name=module.mangle('exports.table'))

  imports = []
  for stmt in self.stmts:
    stmt.emit(module)
    if isinstance(stmt, import_node):
      imports.append(stmt)

  return imports

@program_node.method
def emit_main(self, module):
  with module.add_main():
    box = module.builder.load(module['main'], name='main_box')
    func = module.builder.extract_value(box, 1, name='main_func')
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')
    func_ptr = module.builder.inttoptr(func, T.ptr(T.vfunc(T.ptr(T.box))), name='main_func_ptr')
    module.builder.call(func_ptr, [ret_ptr])
    ret_code = module.builder.call(module.extern('rain_box_to_exit'), [ret_ptr], name='ret_code')
    module.builder.ret(ret_code);

@block_node.method
def emit(self, module):
  for stmt in self.stmts:
    stmt.emit(module)

# helpers

def truthy(module, box):
  typ = module.builder.extract_value(box, 0)
  val = module.builder.extract_value(box, 1)
  not_null = module.builder.icmp_unsigned('!=', typ, T.ityp.null)
  not_zero = module.builder.icmp_unsigned('!=', val, T.i64(0))
  return module.builder.and_(not_null, not_zero)

def static_table_put(module, table_ptr, column_ptr, key_node, key, val):
  table = table_ptr.initializer

  # we can only hash things that are known to this compiler (not addresses)
  if not isinstance(key_node, literal_node):
    print('Unable to hash {!s}'.format(key_node))
    sys.exit(1)
    return

  # get these for storing
  column = T.column([key, val, None])
  column.next = None # for later!
  column.key = key_node

  # compute the hash and allocate a new column for it
  idx = key_node.hash() % HASH_SIZE
  column_ptr.initializer = column

  # update the table array and then update the initializer
  if not isinstance(table.constant[idx], ir.GlobalVariable):
    # no chain
    table.constant[idx] = column_ptr
    table_ptr.initializer = table.type(table.constant)

  else:
    # chain through the list until we find the right key node or the end of the list
    chain = table.constant[idx]
    while chain.initializer.next is not None and chain.initializer.key != key_node:
      chain = chain.initializer.next

    if chain.initializer.key == key_node: # we found the same key node in the list
      save = chain.initializer.next
      chain.initializer.constant[1] = val

      chain.initializer = chain.initializer.type(chain.initializer.constant)
      chain.initializer.next = save
      chain.initializer.key = key_node

    else: # we never found the same key node, but we did find the end of the list
      save = chain.initializer.key
      chain.initializer.constant[2] = column_ptr

      chain.initializer = chain.initializer.type(chain.initializer.constant)
      chain.initializer.next = column_ptr
      chain.initializer.key = save

# statements

@assn_node.method
def emit(self, module):
  ptr = None

  if isinstance(self.lhs, name_node):
    if not module.builder: # global scope
      column_ptr = module.add_global(T.column, name=module.uniq('column'))
      ptr = column_ptr.gep([T.i32(0), T.i32(1)])
      module[self.lhs] = ptr

      key_node = str_node(self.lhs.value)
      key = key_node.emit(module)
      val = self.rhs.emit(module)

      static_table_put(module, module.exports.initializer.source, column_ptr, key_node, key, val)

      module[self.lhs].col = val
      return

    # emit this so a function can't close over its undefined binding
    if self.let:
      with module.builder.goto_entry_block():
        module[self.lhs] = module.builder.alloca(T.box)
        module[self.lhs].bound = False # cheesy hack - see @func_node

    rhs = self.rhs.emit(module)

    if self.lhs not in module:
      raise Exception('Undeclared {!r}'.format(self.lhs))

    module.builder.store(rhs, module[self.lhs])
    module[self.lhs].bound = True

  elif isinstance(self.lhs, idx_node):
    if not module.builder: # global scope
      table_ptr = module[self.lhs.lhs].col.source
      key_node = self.lhs.rhs
      key = key_node.emit(module)
      val = self.rhs.emit(module)

      column_ptr = module.add_global(T.column, name=module.uniq('column'))
      static_table_put(module, table_ptr, column_ptr, key_node, key, val)
      return

    table = self.lhs.lhs.emit(module)
    key = self.lhs.rhs.emit(module)
    val = self.rhs.emit(module)

    with module.builder.goto_entry_block():
      table_ptr = module.builder.alloca(T.box, name='table_ptr')
      key_ptr = module.builder.alloca(T.box, name='key_ptr')
      val_ptr = module.builder.alloca(T.box, name='val_ptr')

    module.builder.store(table, table_ptr)
    module.builder.store(key, key_ptr)
    module.builder.store(val, val_ptr)

    module.builder.call(module.extern('rain_put'), [table_ptr, key_ptr, val_ptr])

@break_node.method
def emit(self, module):
  if not self.cond:
    return module.builder.branch(module.after)

  cond = truthy(module, self.cond.emit(module))
  nobreak = module.builder.append_basic_block('nobreak')
  module.builder.cbranch(cond, module.after, nobreak)
  module.builder.position_at_end(nobreak)

@cont_node.method
def emit(self, module):
  if not self.cond:
    return module.builder.branch(module.before)

  cond = truthy(module, self.cond.emit(module))
  nocont = module.builder.append_basic_block('nocont')
  module.builder.cbranch(cond, module.before, nocont)
  module.builder.position_at_end(nocont)

@if_node.method
def emit(self, module):
  pred = truthy(module, self.pred.emit(module))

  if self.els:
    with module.builder.if_else(pred) as (then, els):
      with then:
        self.body.emit(module)
      with els:
        self.els.emit(module)

  else:
    with module.builder.if_then(truthy(module, self.pred.emit(module))):
      self.body.emit(module)

@loop_node.method
def emit(self, module):
  with module.add_loop() as (before, loop):
    with before:
      module.builder.branch(module.loop)

    with loop:
      self.body.emit(module)
      module.builder.branch(module.loop)

@pass_node.method
def emit(self, module):
  pass

@return_node.method
def emit(self, module):
  if self.value:
    ret = self.value
  else:
    ret = null_node()

  module.builder.store(ret.emit(module), module.ret_ptr)
  module.builder.ret_void()

@save_node.method
def emit(self, module):
  module.builder.store(self.value.emit(module), module.ret_ptr)

@print_node.method
def emit(self, module):
  with module.builder.goto_entry_block():
    val_ptr = module.builder.alloca(T.box, name='val_ptr')

  val = self.value.emit(module)
  module.builder.store(val, val_ptr)
  module.builder.call(module.extern('rain_print'), [val_ptr])

@until_node.method
def emit(self, module):
  with module.add_loop() as (before, loop):
    with before:
      module.builder.cbranch(truthy(module, self.pred.emit(module)), module.after, module.loop)

    with loop:
      self.body.emit(module)
      module.builder.branch(module.before)

@while_node.method
def emit(self, module):
  with module.add_loop() as (before, loop):
    with before:
      module.builder.cbranch(truthy(module, self.pred.emit(module)), module.loop, module.after)

    with loop:
      self.body.emit(module)
      module.builder.branch(module.before)

@for_node.method
def emit(self, module):
  # evaluate the expression and pull out the function pointer
  func_box = self.func.emit(module)
  func_raw = module.builder.extract_value(func_box, 1, name='for_func')
  func_ptr = module.builder.inttoptr(func_raw, T.ptr(T.vfunc(T.ptr(T.box))), name='for_func_ptr')

  # set up the return pointer
  with module.builder.goto_entry_block():
    ret_ptr = module[self.name] =  module.builder.alloca(T.box, name='for_var')

  with module.add_loop() as (before, loop):
    with before:
      # call our function and break if it returns null
      module.builder.store(T.box(None), ret_ptr)
      module.builder.call(func_ptr, [ret_ptr])
      box = module.builder.load(ret_ptr)
      typ = module.builder.extract_value(box, 0)
      not_null = module.builder.icmp_unsigned('!=', typ, T.ityp.null)
      module.builder.cbranch(not_null, module.loop, module.after)

    with loop:
      self.body.emit(module)
      module.builder.branch(module.before)

@import_node.method
def emit(self, module):
  if module.builder: # non-global scope
    print('Can\'t import modules at non-global scope')
    sys.exit(1)

  file = M.Module.find_file(self.name)
  if not file:
    raise Exception('Unable to find module {!r}'.format(self.name))

  qname, mname = M.Module.find_name(file)

  glob = module.add_global(T.box, name=qname + '.exports.table')
  glob.linkage = 'available_externally'

  rename = self.rename or mname

  key_node = str_node(rename)
  key = key_node.emit(module)
  val = static_table_from_ptr(module, glob)

  column_ptr = module.add_global(T.column, name=module.uniq('column'))
  static_table_put(module, module.exports.initializer.source, column_ptr, key_node, key, val)
  ptr = column_ptr.gep([T.i32(0), T.i32(1)])

  module[rename] = ptr
  module[rename].col = val

# expressions

@name_node.method
def emit(self, module):
  if self.value not in module:
    raise Exception('Unknown name {!r}'.format(self.value))

  if not module.builder: # global scope
    return module[self.value].col

  return module.builder.load(module[self.value])

@idx_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t index at global scope') # TODO eventually, you can
    sys.exit(1)

  with module.builder.goto_entry_block():
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')
    table_ptr = module.builder.alloca(T.box, name='table_ptr')
    key_ptr = module.builder.alloca(T.box, name='key_ptr')

  table = self.lhs.emit(module)
  key = self.rhs.emit(module)

  module.builder.store(T.box(None), ret_ptr)
  module.builder.store(table, table_ptr)
  module.builder.store(key, key_ptr)
  module.builder.call(module.extern('rain_get'), [ret_ptr, table_ptr, key_ptr])

  return module.builder.load(ret_ptr)

@null_node.method
def emit(self, module):
  return T.box([T.ityp.null, T.cast.null(0), T.i32(0)])

@int_node.method
def emit(self, module):
  return T.box([T.ityp.int, T.cast.int(self.value), T.i32(0)])

@float_node.method
def emit(self, module):
  val = T.cast.float(self.value).bitcast(T.cast.int)
  return T.box([T.ityp.float, val, T.i32(0)])

@bool_node.method
def emit(self, module):
  return T.box([T.ityp.bool, T.cast.bool(int(self.value)), T.i32(0)])

@str_node.method
def emit(self, module):
  typ = T.arr(T.i8, len(self.value) + 1)
  ptr = module.add_global(typ, name=module.uniq('string'))
  ptr.initializer = typ(bytearray(self.value + '\0', 'utf-8'))
  gep = ptr.gep([T.i32(0), T.i32(0)])

  # need to bullshit around to get this to work - see llvmlite#229
  raw_ir = 'ptrtoint ({0} {1} to {2})'.format(gep.type, gep.get_reference(), T.cast.int)
  val = ir.FormattedConstant(T.cast.int, raw_ir)
  return T.box([T.ityp.str, val, len(self.value)])

def static_table_alloc(module, name, metatable=None):
  # make an empty array of column*
  typ = T.arr(T.ptr(T.column), HASH_SIZE)
  ptr = module.add_global(typ, name=name)
  ptr.initializer = typ([None] * HASH_SIZE)
  return static_table_from_ptr(module, ptr, metatable)

def static_table_from_ptr(module, ptr, metatable=None):
  gep = ptr.gep([T.i32(0), T.i32(0)])

  # cast the array ptr to an i64
  raw_ir = 'ptrtoint ({0} {1} to {2})'.format(gep.type, gep.get_reference(), T.cast.int)
  val = ir.FormattedConstant(T.cast.int, raw_ir)

  if metatable:
    # get these for storing
    mt_val = metatable.emit(module)
    mt_key = module.metatable_key.initializer
    mt_column = T.column([mt_key, mt_val, T.ptr(T.column)(None)])

    # compute hash and allocate a column for it
    mt_idx = str_node('metatable').hash() % HASH_SIZE
    column_ptr = module.add_global(T.column, name=module.uniq('column'))
    column_ptr.initializer = mt_column

    ptr.initializer.constant[mt_idx] = column_ptr
    ptr.initializer = ptr.initializer.type(ptr.initializer.constant)

  # put the i64 in a box
  box = T.box([T.ityp.table, val, 0])
  box.source = ptr # save this for later!
  return box

@table_node.method
def emit(self, module):
  if not module.builder: # global scope
    return static_table_alloc(module, module.uniq('table'), metatable=self.metatable)

  ptr = module.builder.call(module.extern('rain_new_table'), [])

  if self.metatable:
    val = self.metatable.emit(module)

    with module.builder.goto_entry_block():
      val_ptr = module.builder.alloca(T.box, name='key_ptr')

    module.builder.store(val, val_ptr)
    module.builder.call(module.extern('rain_put'), [ptr, module.metatable_key, val_ptr])

  return module.builder.load(ptr)

@func_node.method
def emit(self, module):
  env = OrderedDict()
  for scope in module.scopes[1:]:
    for nm, ptr in scope.items():
      env[nm] = ptr

  if not env:
    typ = T.vfunc(T.ptr(T.box), *[T.ptr(T.box) for x in self.params])

    func = module.add_func(typ)
    func.args[0].add_attribute('sret')

  else:
    env_typ = T.arr(T.box, len(env))
    typ = T.vfunc(T.ptr(env_typ), T.ptr(T.box), *[T.ptr(T.box) for x in self.params])

    func = module.add_func(typ)
    func.args[0].add_attribute('nest')
    func.args[1].add_attribute('sret')

  with module:
    with module.add_func_body(func):
      func_args = func.args

      if env:
        for i, (name, ptr) in enumerate(env.items()):
          gep = module.builder.gep(func_args[0], [T.i32(0), T.i32(i)])
          module[name] = gep

        func_args = func_args[1:]
        module.ret_ptr = func.args[1]

      for name, ptr in zip(self.params, func_args[1:]):
        module[name] = ptr

      self.body.emit(module)

      if not module.builder.block.is_terminated:
        module.builder.ret_void()

  if env:
    env_raw_ptr = module.builder.call(module.extern('GC_malloc'), [T.i32(BOX_SIZE * len(env))])
    env_ptr = module.builder.bitcast(env_raw_ptr, T.ptr(env_typ))

    func = module.add_tramp(func, env_ptr)
    func_i64 = module.builder.ptrtoint(func, T.i64)
    func_box = module.builder.insert_value(T.box([T.ityp.func, T.i64(0), T.i32(0)]), func_i64, 1)

    env_val = env_typ(None)

    for i, (name, ptr) in enumerate(env.items()):
      # cheesy hack - the only time any of these values will ever
      # have a bound value of False will be when it's the item
      # currently being bound, ie, it's this function
      if getattr(ptr, 'bound', None) == False:
        env_val = module.builder.insert_value(env_val, func_box, i)
      else:
        env_val = module.builder.insert_value(env_val, module.builder.load(ptr), i)

    module.builder.store(env_val, env_ptr)

    return func_box

  #val = func.ptrtoint(T.cast.int)
  # need to bullshit around to get this to work - see llvmlite#229
  raw_ir = 'ptrtoint ({0} {1} to {2})'.format(func.type, func.get_reference(), T.cast.int)
  val = ir.FormattedConstant(T.cast.int, raw_ir)
  return T.box([T.ityp.func, val, T.i32(0)])

@extern_node.method
def emit(self, module):
  typ = T.vfunc()
  func = module.find_func(typ, name=self.name.value)

  raw_ir = 'ptrtoint ({0} {1} to {2})'.format(func.type, func.get_reference(), T.cast.int)
  val = ir.FormattedConstant(T.cast.int, raw_ir)
  return T.box([T.ityp.func, val, T.i32(0)])

@call_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t call functions at global scope')
    sys.exit(1)

  with module.builder.goto_entry_block():
    arg_ptrs = [module.builder.alloca(T.box) for arg in self.args]
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')

  func_box = self.func.emit(module)
  arg_boxes = [arg.emit(module) for arg in self.args]

  module.builder.store(T.box(None), ret_ptr)
  for box, ptr in zip(arg_boxes, arg_ptrs):
    module.builder.store(box, ptr)


  func = module.builder.extract_value(func_box, 1, name='func')
  func_ptr = module.builder.inttoptr(func, T.ptr(T.vfunc(T.ptr(T.box), *[T.ptr(T.box) for arg in arg_ptrs])))

  module.builder.call(func_ptr, [ret_ptr] + arg_ptrs)
  ret = module.builder.load(ret_ptr)

  return ret

@meth_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t call methods at global scope')
    sys.exit(1)

  with module.builder.goto_entry_block():
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')
    table_ptr = module.builder.alloca(T.box, name='table_ptr')
    key_ptr = module.builder.alloca(T.box, name='key_ptr')

  table = self.lhs.emit(module)
  key = self.rhs.emit(module)

  module.builder.store(T.box(None), ret_ptr)
  module.builder.store(table, table_ptr)
  module.builder.store(key, key_ptr)
  module.builder.call(module.extern('rain_get'), [ret_ptr, table_ptr, key_ptr])
  func_box = module.builder.load(ret_ptr)

  #

  with module.builder.goto_entry_block():
    arg_ptrs = [module.builder.alloca(T.box) for arg in self.args]
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')

  arg_boxes = [arg.emit(module) for arg in self.args]

  module.builder.store(T.box(None), ret_ptr)
  for box, ptr in zip(arg_boxes, arg_ptrs):
    module.builder.store(box, ptr)


  arg_ptrs = [table_ptr] + arg_ptrs

  func = module.builder.extract_value(func_box, 1, name='func')
  func_ptr = module.builder.inttoptr(func, T.ptr(T.vfunc(T.ptr(T.box), *[T.ptr(T.box) for arg in arg_ptrs])))

  module.builder.call(func_ptr, [ret_ptr] + arg_ptrs)
  ret = module.builder.load(ret_ptr)

  return ret

@bind_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t bind methods at global scope')
    sys.exit(1)

  with module.builder.goto_entry_block():
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')
    table_ptr = module.builder.alloca(T.box, name='table_ptr')
    key_ptr = module.builder.alloca(T.box, name='key_ptr')

  table = self.lhs.emit(module)
  key = self.rhs.emit(module)

  module.builder.store(T.box(None), ret_ptr)
  module.builder.store(table, table_ptr)
  module.builder.store(key, key_ptr)
  module.builder.call(module.extern('rain_get'), [ret_ptr, table_ptr, key_ptr])
  bind_func_box = module.builder.load(ret_ptr)

  env_typ = T.arr(T.box, 2)
  typ = T.vfunc(T.ptr(env_typ), T.ptr(T.box))
  func = module.add_func(typ)
  func.args[0].add_attribute('nest')
  func.args[1].add_attribute('sret')

  with module.add_func_body(func):
    func_ptr = module.builder.gep(func.args[0], [T.i32(0), T.i32(0)])
    self_ptr = module.builder.gep(func.args[0], [T.i32(0), T.i32(1)])

    func_typ = T.vfunc(T.ptr(T.box), T.ptr(T.box))
    real_func_box = module.builder.load(func_ptr)
    real_func = module.builder.extract_value(real_func_box, 1)
    real_func_ptr = module.builder.inttoptr(real_func, T.ptr(func_typ))

    module.builder.call(real_func_ptr, [func.args[1], self_ptr])
    module.builder.ret_void()

  env_raw_ptr = module.builder.call(module.extern('GC_malloc'), [T.i32(BOX_SIZE * 2)])
  env_ptr = module.builder.bitcast(env_raw_ptr, T.ptr(env_typ))
  env_val = env_typ(None)
  env_val = module.builder.insert_value(env_val, bind_func_box, 0)
  env_val = module.builder.insert_value(env_val, table, 1)
  module.builder.store(env_val, env_ptr)

  func = module.add_tramp(func, env_ptr)
  func_i64 = module.builder.ptrtoint(func, T.i64)

  return module.builder.insert_value(T.box([T.ityp.func, T.i64(0), T.i32(0)]), func_i64, 1)

@binary_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t use binary operators at global scope')
    sys.exit(1)

  arith = {
    '+': 'rain_add',
    '-': 'rain_sub',
    '*': 'rain_mul',
    '/': 'rain_div',
    '&': 'rain_and',
    '|': 'rain_or',
    '==': 'rain_eq',
    '!=': 'rain_ne',
    '>': 'rain_gt',
    '>=': 'rain_ge',
    '<': 'rain_lt',
    '<=': 'rain_le',
    '$': 'rain_string_concat',
  }

  with module.builder.goto_entry_block():
    lhs_ptr = module.builder.alloca(T.box, name='lhs_ptr')
    rhs_ptr = module.builder.alloca(T.box, name='rhs_ptr')
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')

  lhs = self.lhs.emit(module)
  rhs = self.rhs.emit(module)

  module.builder.store(lhs, lhs_ptr)
  module.builder.store(rhs, rhs_ptr)
  module.builder.store(T.box(None), ret_ptr)
  module.builder.call(module.extern(arith[self.op]), [ret_ptr, lhs_ptr, rhs_ptr])
  return module.builder.load(ret_ptr)

@unary_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t use unary operators at global scope')
    sys.exit(1)

  arith = {
    '-': 'rain_neg',
    '!': 'rain_not',
  }

  with module.builder.goto_entry_block():
    val_ptr = module.builder.alloca(T.box, name='val_ptr')
    ret_ptr = module.builder.alloca(T.box, name='ret_ptr')

  val = self.val.emit(module)
  module.builder.store(val, val_ptr)
  module.builder.store(T.box(None), ret_ptr)
  module.builder.call(module.extern(arith[self.op]), [ret_ptr, val_ptr])
  return module.builder.load(ret_ptr)

@is_node.method
def emit(self, module):
  if not module.builder: # global scope
    print('Can\'t check types at global scope')
    sys.exit(1)

  lhs = self.lhs.emit(module)
  lhs_typ = module.builder.extract_value(lhs, 0)
  res = module.builder.icmp_unsigned('==', getattr(T.ityp, self.typ.value), lhs_typ)
  res = module.builder.zext(res, T.i64)
  return module.builder.insert_value(T.box([T.ityp.bool, T.i64(0), T.i32(0)]), res, 1)
