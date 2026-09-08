; tree-sitter capture queries for the LocAgent-TS graph builder (typescript grammar).
; Consumed by dependency_graph/ts_build_graph.py -- NOT the repo_index codeblock parser.
; Grammars: tree_sitter_languages.get_language('typescript') / get_language('tsx').
; The 'tsx' grammar is a superset; JSX-only captures live in tsx.scm.
;
; Capture names are grouped by prefix:
;   @def.*     -- an entity that becomes a graph node (function / class)
;   @name      -- the identifier naming the nearest enclosing @def.*
;   @import.*  -- an import/re-export statement (resolved by ts_resolver.py)
;   @call.name -- a call-expression callee (name-matched for the `invokes` edge)
;   @heritage.name -- an extends/implements target (name-matched for `inherits`)

; ─────────────────────────────  functions  ─────────────────────────────
(function_declaration
  name: (identifier) @name) @def.function

(generator_function_declaration
  name: (identifier) @name) @def.function

; const foo = () => {}   /   const foo = function () {}
(variable_declarator
  name: (identifier) @name
  value: [(arrow_function) (function)]) @def.function

; const Foo = forwardRef(...) / memo(...) / observer(...) / React.memo(...)
; -- HOC-wrapped component/function. ts_build_graph digs the inner function out
; of the call arguments; a declarator whose call has no function argument
; (const x = compute()) is dropped.
(variable_declarator
  name: (identifier) @name
  value: (call_expression)) @def.wrapped

; class methods:  foo() {}   /   get foo() {}
(method_definition
  name: (property_identifier) @name) @def.function

; class fields holding a function:  foo = () => {}
(public_field_definition
  name: (property_identifier) @name
  value: [(arrow_function) (function)]) @def.function

; ──────────────────────────────  classes  ─────────────────────────────
(class_declaration
  name: (type_identifier) @name) @def.class

(abstract_class_declaration
  name: (type_identifier) @name) @def.class

; `export default class Foo {}` wraps the declaration; the inner rule still fires.

; ─────────────────────────────  heritage  ────────────────────────────
; class Foo extends Bar implements Baz {}
(class_heritage
  (extends_clause
    value: (identifier) @heritage.name))
(class_heritage
  (extends_clause
    value: (member_expression property: (property_identifier) @heritage.name)))
(class_heritage
  (implements_clause
    (type_identifier) @heritage.name))

; ──────────────────────────────  imports  ────────────────────────────
; import ... from "source"      (import_clause parsed in Python from @import.node)
(import_statement
  source: (string (string_fragment) @import.source)) @import.node

; export { x } from "source"    /   export * from "source"   (barrels)
; also covers `import x = require("source")` (import_require_clause has source:)
(export_statement
  source: (string (string_fragment) @import.source)) @import.node

; ───────────────────────────────  calls  ─────────────────────────────
(call_expression
  function: (identifier) @call.name)

(call_expression
  function: (member_expression
    property: (property_identifier) @call.name))
