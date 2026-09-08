; JSX-only capture deltas for the LocAgent-TS graph builder (tsx grammar).
; Loaded IN ADDITION TO typescript.scm when the file is parsed with the 'tsx'
; grammar (.tsx / .jsx / .js). Consumed by dependency_graph/ts_build_graph.py.
;
;   @jsx.name  -- the tag name of a JSX element. When PascalCase it is
;                 name-matched against component nodes to build the `renders` edge
;                 (the UI analogue of `invokes`). lowercase tags are host DOM
;                 elements and are ignored by the builder.

(jsx_opening_element
  name: (identifier) @jsx.name)

(jsx_self_closing_element
  name: (identifier) @jsx.name)

; <Foo.Bar /> — namespaced / compound components
(jsx_opening_element
  name: (member_expression property: (property_identifier) @jsx.name))

(jsx_self_closing_element
  name: (member_expression property: (property_identifier) @jsx.name))
